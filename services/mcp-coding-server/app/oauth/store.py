"""SQLite-backed OAuth persistence.

Opaque code/token values are stored as SHA-256(pepper + raw value) keys. Raw
values are only returned to the OAuth client and are never persisted.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_auth (txn TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS auth_codes (k TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS access_tokens (k TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS refresh_tokens (k TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
"""
_HASHED_TABLES = ("auth_codes", "access_tokens", "refresh_tokens")


class OAuthStore:
    def __init__(self, db_path: str, pepper: str) -> None:
        self._pepper = pepper.encode("utf-8")
        self._lock = threading.Lock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _hash(self, value: str) -> str:
        return hashlib.sha256(self._pepper + value.encode("utf-8")).hexdigest()

    def put_client(self, client_id: str, data: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO clients(client_id,data) VALUES(?,?)",
                (client_id, data),
            )
            self._conn.commit()

    def get_client(self, client_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM clients WHERE client_id=?", (client_id,)
            ).fetchone()
        return row[0] if row else None

    def put_pending(self, txn: str, client_id: str, params: str, ttl: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_auth(txn,client_id,params,expires_at) VALUES(?,?,?,?)",
                (txn, client_id, params, time.time() + ttl),
            )
            self._conn.commit()

    def take_pending(self, txn: str) -> tuple[str, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT client_id,params,expires_at FROM pending_auth WHERE txn=?",
                (txn,),
            ).fetchone()
            self._conn.execute("DELETE FROM pending_auth WHERE txn=?", (txn,))
            self._conn.commit()
        if not row or row[2] < time.time():
            return None
        return row[0], row[1]

    def put_hashed(self, table: str, raw: str, data: str, ttl: int) -> None:
        assert table in _HASHED_TABLES
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {table}(k,data,expires_at) VALUES(?,?,?)",
                (self._hash(raw), data, time.time() + ttl),
            )
            self._conn.commit()

    def get_hashed(self, table: str, raw: str) -> str | None:
        assert table in _HASHED_TABLES
        with self._lock:
            row = self._conn.execute(
                f"SELECT data,expires_at FROM {table} WHERE k=?",
                (self._hash(raw),),
            ).fetchone()
        if not row or row[1] < time.time():
            return None
        return row[0]

    def delete_hashed(self, table: str, raw: str) -> None:
        assert table in _HASHED_TABLES
        with self._lock:
            self._conn.execute(f"DELETE FROM {table} WHERE k=?", (self._hash(raw),))
            self._conn.commit()
