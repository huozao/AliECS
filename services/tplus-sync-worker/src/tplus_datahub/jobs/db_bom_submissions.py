"""BOM 写入任务的数据库队列与审计事件。"""

from __future__ import annotations

import os
from contextlib import closing
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def _connect():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is empty")
    return psycopg.connect(database_url, connect_timeout=3)


def claim_next_submission() -> dict[str, Any] | None:
    with closing(_connect()) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, draft_id, request_json, requested_by
                       FROM tplus_bom_submissions
                       WHERE status = 'pending'
                       ORDER BY requested_at ASC, id ASC
                       LIMIT 1 FOR UPDATE SKIP LOCKED"""
                )
                row = cur.fetchone()
                if not row:
                    return None
                submission = {
                    "id": int(row[0]), "draft_id": int(row[1]), "request_json": row[2] or {},
                    "requested_by": str(row[3] or ""),
                }
                cur.execute(
                    """UPDATE tplus_bom_submissions
                       SET status='processing', attempts=attempts+1, started_at=NOW(), updated_at=NOW()
                       WHERE id=%s""",
                    (submission["id"],),
                )
                cur.execute(
                    """INSERT INTO tplus_bom_submission_events(submission_id, event_type, detail_json)
                       VALUES (%s, 'processing', '{}'::jsonb)""",
                    (submission["id"],),
                )
                return submission


def add_event(submission_id: int, event_type: str, detail: dict[str, Any]) -> None:
    with closing(_connect()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tplus_bom_submission_events(submission_id, event_type, detail_json) VALUES (%s, %s, %s)",
                (submission_id, event_type, Jsonb(detail)),
            )
        conn.commit()


def finish_submission(
    submission_id: int,
    *,
    status: str,
    response: dict[str, Any] | list[Any] | None = None,
    verification: dict[str, Any] | list[Any] | None = None,
    error: dict[str, Any] | None = None,
    result_bom_id: str | None = None,
) -> None:
    if status not in {"success", "failed", "needs_review"}:
        raise ValueError(f"invalid submission status: {status}")
    with closing(_connect()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE tplus_bom_submissions
                   SET status=%s, response_json=%s, verification_json=%s, error_json=%s,
                       result_bom_id=%s, finished_at=NOW(),
                       verified_at=CASE WHEN %s='success' THEN NOW() ELSE verified_at END,
                       updated_at=NOW()
                   WHERE id=%s""",
                (
                    status, Jsonb(response or {}), Jsonb(verification or {}), Jsonb(error or {}),
                    result_bom_id, status, submission_id,
                ),
            )
            cur.execute(
                "INSERT INTO tplus_bom_submission_events(submission_id, event_type, detail_json) VALUES (%s, %s, %s)",
                (submission_id, status, Jsonb({"result_bom_id": result_bom_id, "error": error or {}})),
            )
            if status == "success":
                cur.execute(
                    """INSERT INTO integration_sync_requests(
                           provider, module, mode, target_json, priority, status, dedupe_key
                       ) VALUES ('chanjet', 'bom', 'incremental', %s, 20, 'pending', %s)
                       ON CONFLICT (dedupe_key) DO NOTHING""",
                    (Jsonb({"submission_id": submission_id}), f"bom-write:{submission_id}"),
                )
        conn.commit()
