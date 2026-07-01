#!/usr/bin/env python3
"""HTTP failover proxy for the ECS-local WebDock reverse SSH tunnels."""

from __future__ import annotations

import http.client
import json
import os
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping


FAILOVER_PREFIX = "▎ ⚠️ 原服务器故障，已自动切换备用服务器，让您久等了。\n\n"


@dataclass(frozen=True)
class ProxyConfig:
    bind_host: str = "127.0.0.1"
    bind_port: int = 11800
    primary_host: str = "127.0.0.1"
    primary_port: int = 11810
    standby_host: str = "127.0.0.1"
    standby_port: int = 11811
    upstream_timeout_seconds: float = 320.0
    primary_down_ttl_seconds: float = 60.0
    failover_prefix: str = FAILOVER_PREFIX


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes


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


def forward_once(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    host: str,
    port: int,
    timeout_seconds: float,
) -> UpstreamResponse:
    conn = http.client.HTTPConnection(host, port, timeout=timeout_seconds)
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
    finally:
        conn.close()


def _mark_primary_down(config: ProxyConfig) -> None:
    global _primary_down_until
    _primary_down_until = time.monotonic() + config.primary_down_ttl_seconds


def _primary_is_down() -> bool:
    return time.monotonic() < _primary_down_until


def _standby_response(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    config: ProxyConfig,
    add_prefix: bool,
) -> UpstreamResponse:
    response = forward_once(
        method,
        path,
        headers,
        body,
        config.standby_host,
        config.standby_port,
        config.upstream_timeout_seconds,
    )
    if add_prefix and 200 <= response.status < 300:
        response = UpstreamResponse(
            response.status,
            response.reason,
            response.headers,
            inject_failover_prefix(response.body, config.failover_prefix),
        )
    return response


def forward_with_failover(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    config: ProxyConfig,
) -> UpstreamResponse:
    if _primary_is_down():
        return _standby_response(method, path, headers, body, config, add_prefix=True)

    try:
        primary = forward_once(
            method,
            path,
            headers,
            body,
            config.primary_host,
            config.primary_port,
            config.upstream_timeout_seconds,
        )
    except (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected):
        _mark_primary_down(config)
        return _standby_response(method, path, headers, body, config, add_prefix=True)

    if is_retryable_webdock_503(primary.status, primary.body):
        _mark_primary_down(config)
        return _standby_response(method, path, headers, body, config, add_prefix=True)
    return primary


class FailoverHandler(BaseHTTPRequestHandler):
    config = parse_config()

    def _proxy(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            response = forward_with_failover(self.command, self.path, self.headers, body, self.config)
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
