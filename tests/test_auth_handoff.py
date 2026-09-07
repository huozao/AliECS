from __future__ import annotations

import base64
import hashlib
import importlib
import os
import uuid
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "services/backend-api"


def module():
    current = sys.modules.get("app")
    if current is not None and not str(getattr(current, "__file__", "")).startswith(str(BACKEND)):
        for key in list(sys.modules):
            if key == "app" or key.startswith("app."):
                del sys.modules[key]
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    return importlib.import_module("app.auth_handoff")


class Cursor:
    def __init__(self, conn): self.cursor = conn.cursor()
    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()
    def execute(self, query, params=()): self.cursor.execute(query.replace("%s", "?"), params)
    def fetchone(self): return self.cursor.fetchone()


class Connection:
    def __init__(self, path): self.conn = sqlite3.connect(path, timeout=10)
    def cursor(self): return Cursor(self.conn)
    def commit(self): self.conn.commit()
    def close(self): self.conn.close()


@pytest.fixture(params=["sqlite", "postgres"])
def store(tmp_path, request):
    mod = module()
    schema = "handoff_test_" + uuid.uuid4().hex
    if request.param == "postgres":
        url = os.getenv("V6_TEST_DATABASE_URL")
        if not url:
            pytest.skip("V6_TEST_DATABASE_URL not configured")
        import psycopg
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
        factory = lambda: psycopg.connect(url, options=f"-c search_path={schema}")
        conn = factory()
        conn.execute("CREATE TABLE auth_browser_handoffs (code_hash TEXT PRIMARY KEY, challenge TEXT NOT NULL, origin TEXT NOT NULL, session_token TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL)")
        conn.commit()
        conn.close()
        yield mod.HandoffStore(factory)
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
    else:
        path = tmp_path / "handoffs.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE auth_browser_handoffs (code_hash TEXT PRIMARY KEY, challenge TEXT NOT NULL, origin TEXT NOT NULL, session_token TEXT NOT NULL, expires_at REAL NOT NULL)")
        conn.close()
        yield mod.HandoffStore(lambda: Connection(path))


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


VERIFIER = "a" * 43
ORIGIN = "https://market.hydwang.xyz"


def test_short_code_is_bound_once_and_preserves_session(store):
    code = store.issue(token="test-session", challenge=challenge(VERIFIER), origin=ORIGIN, now=100)
    assert code != "test-session"
    assert store.consume(code=code, verifier="b" * 43, origin=ORIGIN, now=110) is None
    assert store.consume(code=code, verifier=VERIFIER, origin="https://evil.example", now=110) is None
    assert store.consume(code=code, verifier=VERIFIER, origin=ORIGIN, now=110) == "test-session"
    assert store.consume(code=code, verifier=VERIFIER, origin=ORIGIN, now=111) is None


def test_exact_sixty_second_expiry_and_invalid_challenge(store):
    code = store.issue(token="session", challenge=challenge(VERIFIER), origin=ORIGIN, now=100)
    assert store.consume(code=code, verifier=VERIFIER, origin=ORIGIN, now=160) is None
    with pytest.raises(ValueError): store.issue(token="session", challenge="short", origin=ORIGIN, now=100)


def test_independent_workers_consume_only_once(store):
    code = store.issue(token="session", challenge=challenge(VERIFIER), origin=ORIGIN, now=100)
    def consume(_): return store.consume(code=code, verifier=VERIFIER, origin=ORIGIN, now=101)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(consume, range(8)))
    assert results.count("session") == 1
    assert results.count(None) == 7


def test_handoff_http_rejects_wrong_origin_and_does_not_cache(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    mod = module()
    router = importlib.import_module("app.routers.auth_oidc")
    monkeypatch.setenv("OIDC_ENABLED", "true")
    monkeypatch.setattr(router, "HandoffStore", lambda _: store)
    app = FastAPI()
    app.include_router(router.router)
    code = store.issue(token="synthetic-session", challenge=challenge(VERIFIER), origin=ORIGIN)
    with TestClient(app) as client:
        body = {"code": code, "verifier": VERIFIER}
        assert client.post("/v1/auth/oidc/handoff", json=body).status_code == 400
        assert client.post("/v1/auth/oidc/handoff", json=body, headers={"Origin": "https://evil.example"}).status_code == 400
        response = client.post("/v1/auth/oidc/handoff", json=body, headers={"Origin": ORIGIN})
        assert response.status_code == 200
        assert response.json() == {"token": "synthetic-session"}
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/v1/auth/oidc/handoff", json=body, headers={"Origin": ORIGIN}).status_code == 400
