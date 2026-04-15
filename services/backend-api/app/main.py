from __future__ import annotations

import os
from contextlib import closing

import psycopg
from fastapi import FastAPI

app = FastAPI(title="backend-api-phase1", version="0.1.0")


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "")


def _db_ping() -> tuple[bool, str]:
    database_url = _database_url()
    if not database_url:
        return False, "DATABASE_URL is empty"

    try:
        with closing(psycopg.connect(database_url, connect_timeout=3)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, "db ok"
    except Exception as exc:  # pragma: no cover - defensive path
        return False, f"db error: {exc}"


@app.get("/healthz")
def healthz() -> dict[str, object]:
    db_ok, db_message = _db_ping()
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "backend-api",
        "database": {
            "ok": db_ok,
            "message": db_message,
        },
    }


@app.get("/readyz")
def readyz() -> dict[str, object]:
    db_ok, db_message = _db_ping()
    if not db_ok:
        return {
            "status": "not-ready",
            "reason": db_message,
        }

    return {"status": "ready"}


@app.get("/v1/ping")
def ping() -> dict[str, str]:
    return {"message": "pong"}
