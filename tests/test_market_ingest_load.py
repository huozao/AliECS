"""Opt-in sustained single-worker market-write isolation evidence."""
from __future__ import annotations

import http.client
import os
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import uvicorn
from fastapi import FastAPI


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "backend-api"))


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(port: int, path: str, *, method: str = "GET", body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, float]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    started = time.monotonic()
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        response.read()
        return response.status, (time.monotonic() - started) * 1000
    finally:
        connection.close()


def test_opt_in_ten_minute_market_write_login_and_read_load():
    duration = float(os.getenv("V6_MARKET_LOAD_SECONDS", "0"))
    if duration <= 0:
        pytest.skip("set V6_MARKET_LOAD_SECONDS=600 for sustained local load evidence")
    from app import core
    from app.routers import auth_oidc, market_snapshot

    written = 0
    write_errors: list[BaseException] = []
    stop = threading.Event()

    def synthetic_commit(_: dict) -> dict:
        nonlocal written
        # A normal isolated write is deliberately nonzero, but does not emulate
        # an unbounded stall or acknowledge before its synthetic commit point.
        time.sleep(0.05)
        written += 1
        return {"ok": True, "run_id": "load-run", "sequence": written}

    app = FastAPI()
    app.include_router(market_snapshot.router)
    app.include_router(auth_oidc.router)
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, workers=1,
                                           access_log=False, log_level="critical"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + 3
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    body = (b'{"schema_version":"market-review.v1","model_version":"V6.0",'
            b'"run_id":"load-run","sequence":1,"published_at":"2026-09-10T00:00:00Z",'
            b'"quotes":[],"bands":[],"events":[]}')
    env = {"AUTH_TOKEN_SECRET": "x" * 32, "OIDC_ENABLED": "true",
           "OIDC_ISSUER": "https://auth.example.test", "OIDC_CLIENT_ID": "test-client",
           "OIDC_REDIRECT_URI": "https://website.example.test/callback",
           "MARKET_SNAPSHOT_INGEST_TOKEN": "test-ingest-token"}
    discovery = {"authorization_endpoint": "https://auth.example.test/authorize"}
    try:
        with patch.dict(os.environ, env, clear=False), \
                patch.object(market_snapshot.market_review, "ingest", side_effect=synthetic_commit), \
                patch.object(market_snapshot.market_review, "realtime_view", return_value={"ok": True}), \
                patch.object(auth_oidc, "_http_get_json", return_value=discovery), \
                patch.object(core, "_current_token_version", return_value=1):
            read_token = core._encode_token({"uid": 7, "sub": "load", "tv": 1,
                                             "permissions": ["market.read"],
                                             "exp": int(time.time()) + 3600})
            def writer() -> None:
                while not stop.is_set():
                    try:
                        status, _ = _request(port, "/v1/internal/market/snapshot", method="POST", body=body,
                                             headers={"Content-Type": "application/json",
                                                      "X-Market-Snapshot-Token": "test-ingest-token"})
                        if status != 200:
                            raise AssertionError(f"write status {status}")
                    except BaseException as exc:  # surfaced on the test thread
                        write_errors.append(exc)
                        return
                    stop.wait(0.95)

            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            login_ms, read_ms = [], []
            end = time.monotonic() + duration
            while time.monotonic() < end:
                status, elapsed = _request(port, "/v1/auth/oidc/login")
                assert status == 302
                login_ms.append(elapsed)
                status, elapsed = _request(port, "/v1/market/realtime",
                                            headers={"Authorization": f"Bearer {read_token}"})
                assert status == 200
                read_ms.append(elapsed)
                time.sleep(5)
            stop.set()
            thread.join(3)
            assert not write_errors
            assert written >= max(1, int(duration * 0.7))
            for name, samples in (("login", login_ms), ("realtime", read_ms)):
                ordered = sorted(samples)
                line = "sustained_market_load: duration_s=%.0f writes=%d %s_samples=%d %s_ms_p50=%.3f %s_ms_p95=%.3f %s_ms_max=%.3f" % (
                    duration, written, name, len(samples), name, ordered[len(ordered) // 2],
                    name, ordered[min(len(ordered) - 1, int(len(ordered) * .95))], name, max(ordered))
                print(line)
                evidence_path = os.getenv("V6_MARKET_LOAD_EVIDENCE")
                if evidence_path:
                    with open(evidence_path, "a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
    finally:
        stop.set()
        server.should_exit = True
        server_thread.join(3)
