from __future__ import annotations

import os
from contextlib import closing
from typing import Any

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency guard for local partial installs
    psycopg = None

try:
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover
    Jsonb = lambda value: value  # type: ignore


def connect_if_configured() -> Any | None:
    if psycopg is None:
        return None
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return None
    return psycopg.connect(database_url, connect_timeout=3)


def fetch_next_bom_request(conn: Any | None = None, limit: int = 5) -> dict[str, Any] | None:
    if conn is None:
        owned_conn = connect_if_configured()
        if owned_conn is None:
            return None
        with closing(owned_conn):
            return fetch_next_bom_request(owned_conn, limit=limit)

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, mode, target_json, reason_event_id
                FROM integration_sync_requests
                WHERE provider = 'chanjet'
                  AND module = 'bom'
                  AND status = 'pending'
                ORDER BY priority ASC, requested_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (),
            )
            row = cur.fetchone()
            if not row:
                return None
            request = {
                "id": int(row[0]),
                "mode": row[1],
                "target_json": row[2] or {},
                "reason_event_id": row[3],
            }
            cur.execute(
                """
                UPDATE integration_sync_requests
                SET status = 'running', started_at = NOW(), updated_at = NOW()
                WHERE id = %s
                """,
                (request["id"],),
            )
            return request


def fetch_sync_config(provider: str = "chanjet", conn: Any | None = None) -> dict[str, Any] | None:
    """读取定时同步配置（enabled / interval_seconds / anchor_time）。无 DB / 无行 / 表不存在
    均返回 None，由调用方回退到 env 默认。"""
    if conn is None:
        owned_conn = connect_if_configured()
        if owned_conn is None:
            return None
        with closing(owned_conn):
            return fetch_sync_config(provider, owned_conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled, interval_seconds, anchor_time FROM integration_sync_config WHERE provider = %s",
            (provider,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "enabled": bool(row[0]),
            "interval_seconds": int(row[1]),
            "anchor_time": str(row[2] or ""),
        }


def fetch_last_scheduled_full_at(provider: str = "chanjet", conn: Any | None = None) -> Any | None:
    """上一次定时全量同步的开始时刻（aware-UTC）。用于重启后不丢锚点相位：
    容器重建不应该在白天立刻补跑一次全量。无 DB / 无记录返回 None。"""
    if conn is None:
        owned_conn = connect_if_configured()
        if owned_conn is None:
            return None
        with closing(owned_conn):
            return fetch_last_scheduled_full_at(provider, owned_conn)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT started_at FROM integration_sync_runs
            WHERE provider = %s AND mode = 'scheduled_full' AND started_at IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (provider,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def finish_bom_request(request_id: int, status: str, exit_code: int, detail: dict[str, Any]) -> None:
    conn = connect_if_configured()
    if conn is None:
        return
    with closing(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO integration_sync_runs(provider, module, mode, status, finished_at, exit_code, detail_json, error_json)
                VALUES ('chanjet', 'bom', %s, %s, NOW(), %s, %s, %s)
                RETURNING id
                """,
                (
                    str(detail.get("mode") or "incremental"),
                    status,
                    exit_code,
                    Jsonb(detail),
                    Jsonb({} if status == "success" else detail),
                ),
            )
            run_id = int(cur.fetchone()[0])
            cur.execute(
                """
                UPDATE integration_sync_requests
                SET status = %s,
                    finished_at = NOW(),
                    sync_run_id = %s,
                    error_json = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (status, run_id, Jsonb({} if status == "success" else detail), request_id),
            )
        conn.commit()
