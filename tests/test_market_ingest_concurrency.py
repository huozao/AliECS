"""Single-worker regression coverage for market ingest isolation."""
from __future__ import annotations

import asyncio
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


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(port: int, method: str, path: str, *, body: bytes | None = None,
             headers: dict[str, str] | None = None, timeout: float = 1.0) -> tuple[int, float]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    started = time.monotonic()
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        response.read()
        return response.status, time.monotonic() - started
    finally:
        connection.close()


def test_single_worker_keeps_oidc_login_responsive_while_market_ingest_is_slow():
    """A slow committed write cannot occupy the sole Uvicorn event loop."""
    from app.routers import auth_oidc, market_snapshot

    started = threading.Event()
    release = threading.Event()
    ingest_result: list[tuple[int, float]] = []

    def slow_ingest(_: dict) -> dict:
        started.set()
        assert release.wait(2), "test release timer did not fire"
        return {"ok": True, "run_id": "load-run", "sequence": 1}

    app = FastAPI()
    app.include_router(market_snapshot.router)
    app.include_router(auth_oidc.router)
    port = _unused_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, workers=1,
                                           access_log=False, log_level="critical"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + 3
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "single-worker Uvicorn did not start"

    body = (b'{"schema_version":"market-review.v1","model_version":"V6.0",'
            b'"run_id":"load-run","sequence":1,"published_at":"2026-09-10T00:00:00Z",'
            b'"quotes":[],"bands":[],"events":[]}')
    oidc_env = {
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "https://auth.example.test",
        "OIDC_CLIENT_ID": "test-client",
        "OIDC_REDIRECT_URI": "https://website.example.test/callback",
        "MARKET_SNAPSHOT_INGEST_TOKEN": "test-ingest-token",
    }
    discovery = {"authorization_endpoint": "https://auth.example.test/authorize"}
    try:
        with patch.dict(os.environ, oidc_env, clear=False), \
                patch.object(market_snapshot.market_review, "ingest", side_effect=slow_ingest), \
                patch.object(auth_oidc, "_http_get_json", return_value=discovery):
            auth_oidc._pending_states.clear()
            auth_oidc._discovery_cache.clear()
            ingest_thread = threading.Thread(
                target=lambda: ingest_result.append(_request(
                    port, "POST", "/v1/internal/market/snapshot", body=body,
                    headers={"Content-Type": "application/json",
                             "X-Market-Snapshot-Token": "test-ingest-token"}, timeout=3)),
                daemon=True,
            )
            ingest_thread.start()
            assert started.wait(1), "slow market ingest did not begin"
            timer = threading.Timer(0.8, release.set)
            timer.start()
            login_status, login_elapsed = _request(
                port, "GET", "/v1/auth/oidc/login", timeout=0.4)
            assert login_status == 302
            assert login_elapsed < 0.4
            ingest_thread.join(2)
            assert ingest_result and ingest_result[0][0] == 200
    finally:
        release.set()
        server.should_exit = True
        server_thread.join(3)


def test_cancelled_client_keeps_market_ingest_capacity_until_write_finishes():
    """Cancellation cannot admit a second write while the first transaction runs."""
    from app.routers import market_snapshot

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_write() -> dict:
        started.set()
        assert release.wait(2)
        finished.set()
        return {"ok": True}

    async def scenario() -> None:
        task = asyncio.create_task(market_snapshot._submit_market_ingest(slow_write, "review"))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(Exception) as busy:
            await market_snapshot._submit_market_ingest(lambda: {"ok": True}, "review")
        assert getattr(busy.value, "status_code", None) == 503
        assert getattr(busy.value, "headers", {}).get("Retry-After") == "1"
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        result = await market_snapshot._submit_market_ingest(lambda: {"ok": True}, "review")
        assert result == {"ok": True}

    asyncio.run(scenario())
