#!/usr/bin/env python3
"""HTTP failover proxy for the ECS-local WebDock reverse SSH tunnels."""

from __future__ import annotations

import http.client
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator, Mapping


FAILOVER_PREFIX = "▎ ⚠️ 原服务器故障，已自动切换备用服务器，让您久等了。\n\n"


@dataclass(frozen=True)
class ProxyConfig:
    bind_host: str = "127.0.0.1"
    bind_port: int = 11800
    primary_host: str = "127.0.0.1"
    primary_port: int = 11810
    standby_host: str = "127.0.0.1"
    standby_port: int = 11811
    # Device names surfaced to clients via X-Webdock-Device (e.g. webdock1);
    # keep them in /etc/default/webdock-failover-proxy next to the port mapping.
    primary_name: str = ""
    standby_name: str = ""
    upstream_timeout_seconds: float = 320.0
    primary_down_ttl_seconds: float = 60.0
    failover_prefix: str = FAILOVER_PREFIX
    ledger_path: str = "/var/lib/webdock-failover-proxy/requests.sqlite3"
    ledger_retention_seconds: int = 604800


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes


class PreSubmitConnectionError(Exception):
    """The TCP connection was never established; no upstream request was sent."""


class DeliveryUnknownError(Exception):
    """The connection was established but the final delivery state is unknown."""


class RequestLedgerConflict(Exception):
    pass


class RequestLedgerBusy(Exception):
    pass


_primary_down_until = 0.0


def _int_from_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def _float_from_env(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def parse_config(env: Mapping[str, str] | None = None) -> ProxyConfig:
    source = os.environ if env is None else env
    return ProxyConfig(
        bind_host=source.get("WEBDOCK_FAILOVER_BIND_HOST", ProxyConfig.bind_host).strip()
        or ProxyConfig.bind_host,
        bind_port=_int_from_env(source, "WEBDOCK_FAILOVER_BIND_PORT", ProxyConfig.bind_port),
        primary_host=source.get("WEBDOCK_FAILOVER_PRIMARY_HOST", ProxyConfig.primary_host).strip()
        or ProxyConfig.primary_host,
        primary_port=_int_from_env(source, "WEBDOCK_FAILOVER_PRIMARY_PORT", ProxyConfig.primary_port),
        standby_host=source.get("WEBDOCK_FAILOVER_STANDBY_HOST", ProxyConfig.standby_host).strip()
        or ProxyConfig.standby_host,
        standby_port=_int_from_env(source, "WEBDOCK_FAILOVER_STANDBY_PORT", ProxyConfig.standby_port),
        primary_name=source.get("WEBDOCK_FAILOVER_PRIMARY_NAME", ProxyConfig.primary_name).strip(),
        standby_name=source.get("WEBDOCK_FAILOVER_STANDBY_NAME", ProxyConfig.standby_name).strip(),
        upstream_timeout_seconds=_float_from_env(
            source,
            "WEBDOCK_FAILOVER_UPSTREAM_TIMEOUT_SECONDS",
            ProxyConfig.upstream_timeout_seconds,
        ),
        primary_down_ttl_seconds=_float_from_env(
            source,
            "WEBDOCK_FAILOVER_PRIMARY_DOWN_TTL_SECONDS",
            ProxyConfig.primary_down_ttl_seconds,
        ),
        failover_prefix=source.get("WEBDOCK_FAILOVER_PREFIX", ProxyConfig.failover_prefix),
        ledger_path=source.get("WEBDOCK_FAILOVER_LEDGER_PATH", ProxyConfig.ledger_path).strip()
        or ProxyConfig.ledger_path,
        ledger_retention_seconds=_int_from_env(
            source,
            "WEBDOCK_FAILOVER_LEDGER_RETENTION_SECONDS",
            ProxyConfig.ledger_retention_seconds,
        ),
    )


def is_retryable_webdock_503(status: int, body: bytes) -> bool:
    if status != 503:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = body.decode("utf-8", errors="ignore")
    else:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            text = str(detail.get("message", ""))
        else:
            text = str(detail or "")
    return "Chrome not running or CDP attach failed" in text or "CDP attach failed" in text


def inject_failover_prefix(body: bytes, prefix: str) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        return body
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        return body
    message["content"] = prefix + message["content"]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _filtered_request_headers(headers: Mapping[str, str], body: bytes, host: str, port: int) -> dict[str, str]:
    blocked = {"host", "connection", "content-length", "transfer-encoding"}
    result = {key: value for key, value in headers.items() if key.lower() not in blocked}
    result["Host"] = f"{host}:{port}"
    result["Connection"] = "close"
    result["Content-Length"] = str(len(body))
    return result


def _filtered_response_headers(headers: Mapping[str, str], body: bytes) -> dict[str, str]:
    blocked = {"connection", "content-length", "transfer-encoding"}
    result = {key: value for key, value in headers.items() if key.lower() not in blocked}
    result["Content-Length"] = str(len(body))
    return result


def request_payload_hash(method: str, path: str, body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(method.upper().encode("ascii", errors="ignore"))
    digest.update(b"\0")
    digest.update(path.encode("utf-8"))
    digest.update(b"\0")
    digest.update(body)
    return digest.hexdigest()


class RequestLedger:
    def __init__(self, path: str, retention_seconds: int) -> None:
        self.path = path
        self.retention_seconds = retention_seconds
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            if self.path != ":memory:":
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    route TEXT,
                    response_status INTEGER,
                    response_reason TEXT,
                    response_headers TEXT,
                    response_body BLOB,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "DELETE FROM requests WHERE updated_at < ?",
                (time.time() - self.retention_seconds,),
            )

    def claim(self, request_id: str, payload_hash: str) -> UpstreamResponse | None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO requests(request_id, payload_hash, state, updated_at)
                    VALUES (?, ?, 'assigned', ?)
                    """,
                    (request_id, payload_hash, now),
                )
                return None
            if row["payload_hash"] != payload_hash:
                raise RequestLedgerConflict(f"request_id {request_id} was reused with a different payload")
            if row["state"] == "completed":
                headers = json.loads(row["response_headers"] or "{}")
                headers["X-Request-Ledger"] = "cached"
                return UpstreamResponse(
                    int(row["response_status"]),
                    str(row["response_reason"] or ""),
                    headers,
                    bytes(row["response_body"] or b""),
                )
            if row["state"] == "pre_submit_failed":
                connection.execute(
                    """
                    UPDATE requests
                    SET state = 'assigned', route = NULL, updated_at = ?
                    WHERE request_id = ?
                    """,
                    (now, request_id),
                )
                return None
            raise RequestLedgerBusy(
                f"request_id {request_id} is {row['state']}; refusing an automatic resend"
            )

    def mark(self, request_id: str, state: str, route: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE requests
                SET state = ?, route = COALESCE(?, route), updated_at = ?
                WHERE request_id = ?
                """,
                (state, route, time.time(), request_id),
            )

    def complete(self, request_id: str, route: str, response: UpstreamResponse) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE requests
                SET state = 'completed', route = ?, response_status = ?,
                    response_reason = ?, response_headers = ?, response_body = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (
                    route,
                    response.status,
                    response.reason,
                    json.dumps(response.headers, ensure_ascii=False),
                    response.body,
                    time.time(),
                    request_id,
                ),
            )


def forward_once(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    host: str,
    port: int,
    timeout_seconds: float,
    on_connected: object | None = None,
) -> UpstreamResponse:
    conn = http.client.HTTPConnection(host, port, timeout=timeout_seconds)
    try:
        try:
            conn.connect()
        except Exception as exc:
            raise PreSubmitConnectionError(f"{host}:{port} connect failed: {exc}") from exc
        if callable(on_connected):
            on_connected()
        try:
            conn.request(method, path, body=body, headers=_filtered_request_headers(headers, body, host, port))
            response = conn.getresponse()
            response_body = response.read()
            return UpstreamResponse(
                status=response.status,
                reason=response.reason,
                headers={key: value for key, value in response.getheaders()},
                body=response_body,
            )
        except Exception as exc:
            raise DeliveryUnknownError(f"{host}:{port} disconnected after connect: {exc}") from exc
    finally:
        conn.close()


def _mark_primary_down(config: ProxyConfig) -> None:
    global _primary_down_until
    _primary_down_until = time.monotonic() + config.primary_down_ttl_seconds


def _primary_is_down() -> bool:
    return time.monotonic() < _primary_down_until


def annotate_route(response: UpstreamResponse, config: ProxyConfig, route: str) -> UpstreamResponse:
    """Stamp which upstream served the request (X-Webdock-Route/-Device) so
    downstream consumers (openclaw-bridge card footer) can show the source."""
    headers = dict(response.headers)
    headers["X-Webdock-Route"] = route
    name = config.primary_name if route == "primary" else config.standby_name
    if name:
        headers["X-Webdock-Device"] = name
    return UpstreamResponse(response.status, response.reason, headers, response.body)


def _standby_response(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    config: ProxyConfig,
    add_prefix: bool,
    timeout_seconds: float,
    on_connected: object | None = None,
) -> UpstreamResponse:
    response = forward_once(
        method,
        path,
        headers,
        body,
        config.standby_host,
        config.standby_port,
        timeout_seconds,
        on_connected,
    )
    if add_prefix and 200 <= response.status < 300:
        response = UpstreamResponse(
            response.status,
            response.reason,
            response.headers,
            inject_failover_prefix(response.body, config.failover_prefix),
        )
    return annotate_route(response, config, "standby")


def forward_with_failover(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    config: ProxyConfig,
    ledger: RequestLedger | None = None,
) -> UpstreamResponse:
    request_path = path.split("?", 1)[0].rstrip("/")
    requires_ledger = method.upper() == "POST" and request_path in {
        "/chat/completions",
        "/v1/chat/completions",
    }
    request_id = ""
    for key, value in headers.items():
        if key.lower() == "x-request-id":
            request_id = str(value).strip()
            break
    active_ledger = ledger
    if requires_ledger:
        if not request_id:
            raise RequestLedgerConflict("X-Request-ID is required for chat completions")
        active_ledger = active_ledger or RequestLedger(config.ledger_path, config.ledger_retention_seconds)
        cached = active_ledger.claim(request_id, request_payload_hash(method, path, body))
        if cached is not None:
            return cached

    started = time.monotonic()

    def remaining_timeout() -> float:
        remaining = config.upstream_timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise PreSubmitConnectionError("shared upstream deadline exhausted before fallback")
        return remaining

    def mark(state: str, route: str | None = None) -> None:
        if active_ledger and request_id:
            active_ledger.mark(request_id, state, route)

    def send_standby() -> UpstreamResponse:
        mark("assigned", "standby")
        try:
            response = _standby_response(
                method,
                path,
                headers,
                body,
                config,
                add_prefix=True,
                timeout_seconds=remaining_timeout(),
                on_connected=lambda: mark("accepted", "standby"),
            )
        except PreSubmitConnectionError:
            mark("pre_submit_failed", "standby")
            raise
        except DeliveryUnknownError:
            mark("unknown", "standby")
            raise
        if active_ledger and request_id:
            active_ledger.complete(request_id, "standby", response)
        return response

    if _primary_is_down():
        return send_standby()

    mark("assigned", "primary")
    try:
        primary = forward_once(
            method,
            path,
            headers,
            body,
            config.primary_host,
            config.primary_port,
            remaining_timeout(),
            lambda: mark("accepted", "primary"),
        )
    except PreSubmitConnectionError:
        _mark_primary_down(config)
        return send_standby()
    except DeliveryUnknownError:
        mark("unknown", "primary")
        raise

    if is_retryable_webdock_503(primary.status, primary.body):
        _mark_primary_down(config)
        return send_standby()
    response = annotate_route(primary, config, "primary")
    if active_ledger and request_id:
        active_ledger.complete(request_id, "primary", response)
    return response


class FailoverHandler(BaseHTTPRequestHandler):
    config = parse_config()
    ledger: RequestLedger | None = None

    def _proxy(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            response = forward_with_failover(
                self.command,
                self.path,
                self.headers,
                body,
                self.config,
                self.ledger,
            )
        except RequestLedgerConflict as exc:
            payload = json.dumps(
                {"detail": {"message": str(exc), "error_code": "request_id_conflict"}},
                ensure_ascii=False,
            ).encode("utf-8")
            response = UpstreamResponse(409, "Conflict", {"Content-Type": "application/json"}, payload)
        except RequestLedgerBusy as exc:
            payload = json.dumps(
                {"detail": {"message": str(exc), "error_code": "request_delivery_not_retryable"}},
                ensure_ascii=False,
            ).encode("utf-8")
            response = UpstreamResponse(409, "Conflict", {"Content-Type": "application/json"}, payload)
        except DeliveryUnknownError as exc:
            payload = json.dumps(
                {"detail": {"message": str(exc), "error_code": "request_delivery_unknown"}},
                ensure_ascii=False,
            ).encode("utf-8")
            response = UpstreamResponse(
                503,
                "Service Unavailable",
                {"Content-Type": "application/json", "X-Request-Delivery": "unknown"},
                payload,
            )
        except Exception as exc:
            payload = json.dumps(
                {"detail": {"message": f"WebDock failover proxy upstream failure: {exc}"}},
                ensure_ascii=False,
            ).encode("utf-8")
            response = UpstreamResponse(
                503,
                "Service Unavailable",
                {"Content-Type": "application/json"},
                payload,
            )
        self.send_response(response.status, response.reason)
        for key, value in _filtered_response_headers(response.headers, response.body).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    def do_GET(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def serve_forever(config: ProxyConfig) -> None:
    FailoverHandler.config = config
    FailoverHandler.ledger = RequestLedger(config.ledger_path, config.ledger_retention_seconds)
    with ThreadingHTTPServer((config.bind_host, config.bind_port), FailoverHandler) as server:
        print(
            "webdock-failover-proxy listening on "
            f"{config.bind_host}:{config.bind_port} -> "
            f"primary {config.primary_host}:{config.primary_port}, "
            f"standby {config.standby_host}:{config.standby_port}",
            flush=True,
        )
        server.serve_forever()


def main() -> None:
    serve_forever(parse_config())


if __name__ == "__main__":
    main()
