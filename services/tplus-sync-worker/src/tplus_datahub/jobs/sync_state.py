from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from typing import Any

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

try:
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover
    Jsonb = lambda value: value  # type: ignore


def snapshot_bom_rows(rows: list[Any]) -> dict[str, Any]:
    normalized = [_normalize_row(row) for row in rows]
    normalized.sort(key=lambda row: row["record_key"])
    snapshot_hash = _stable_hash(normalized)
    return {
        "row_count": len(rows),
        "snapshot_hash": snapshot_hash,
        "records": normalized,
    }


def build_snapshot_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any] | None:
    if previous is None or previous.get("snapshot_hash") == current.get("snapshot_hash"):
        return None
    previous_count = int(previous.get("row_count") or 0)
    current_count = int(current.get("row_count") or 0)
    return {
        "status": "needs_review",
        "severity": "warning",
        "summary": f"BOM full snapshot changed: rows {previous_count} -> {current_count}",
        "diff_json": {
            "previous_snapshot_id": previous.get("id"),
            "current_snapshot_id": current.get("id"),
            "previous_hash": previous.get("snapshot_hash"),
            "current_hash": current.get("snapshot_hash"),
            "previous_row_count": previous_count,
            "current_row_count": current_count,
            "row_count_delta": current_count - previous_count,
        },
    }


def record_bom_snapshot_if_configured(rows: list[Any], *, mode: str, source_json: dict[str, Any] | None = None) -> None:
    if psycopg is None:
        return
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return
    snapshot = snapshot_bom_rows(rows)
    source = dict(source_json or {})
    source["mode"] = mode
    try:
        with closing(psycopg.connect(database_url, connect_timeout=3)) as conn:
            previous = _latest_full_snapshot(conn) if mode in {"full_bom", "scheduled_full"} else None
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO integration_sync_snapshots(provider, module, mode, row_count, snapshot_hash, source_json)
                    VALUES ('chanjet', 'bom', %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (mode, snapshot["row_count"], snapshot["snapshot_hash"], Jsonb(source)),
                )
                snapshot_id = int(cur.fetchone()[0])
                snapshot["id"] = snapshot_id
                if mode in {"full_bom", "scheduled_full"}:
                    diff = build_snapshot_diff(previous, snapshot)
                    if diff is not None:
                        cur.execute(
                            """
                            INSERT INTO integration_reconciliation_diffs(
                                provider, module, status, severity, summary, diff_json,
                                full_snapshot_id, incremental_snapshot_id
                            )
                            VALUES ('chanjet', 'bom', %s, %s, %s, %s, %s, NULL)
                            """,
                            (
                                diff["status"],
                                diff["severity"],
                                diff["summary"],
                                Jsonb(diff["diff_json"]),
                                snapshot_id,
                            ),
                        )
            conn.commit()
    except Exception:
        return


def record_tplus_sync_run_if_configured(
    *,
    module: str,
    mode: str,
    status: str,
    row_count: int = 0,
    exit_code: int | None = None,
    detail_json: dict[str, Any] | None = None,
    error_json: dict[str, Any] | None = None,
) -> int | None:
    if psycopg is None:
        return None
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return None
    try:
        with closing(psycopg.connect(database_url, connect_timeout=3)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO integration_sync_runs(
                        provider, module, mode, status, finished_at, row_count,
                        exit_code, detail_json, error_json
                    )
                    VALUES ('chanjet', %s, %s, %s, NOW(), %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        module,
                        mode,
                        status,
                        row_count,
                        exit_code,
                        Jsonb(detail_json or {}),
                        Jsonb(error_json or {}),
                    ),
                )
                run_id = int(cur.fetchone()[0])
            conn.commit()
            return run_id
    except Exception:
        return None


def _latest_full_snapshot(conn: Any) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, row_count, snapshot_hash
            FROM integration_sync_snapshots
            WHERE provider = 'chanjet'
              AND module = 'bom'
              AND mode IN ('full_bom', 'scheduled_full')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "row_count": row[1], "snapshot_hash": row[2]}


def _normalize_row(row: Any) -> dict[str, Any]:
    record_key = _record_key(row)
    return {"record_key": record_key, "record_hash": _stable_hash(row), "raw": row}


def _record_key(row: Any) -> str:
    if isinstance(row, dict):
        parts = [
            row.get("ID") or row.get("id"),
            row.get("Code") or row.get("code"),
            row.get("Version") or row.get("version"),
            row.get("Disabled") or row.get("disabled"),
        ]
        if any(part not in (None, "") for part in parts):
            return "|".join("" if part is None else str(part) for part in parts)
    return _stable_hash(row)


def _stable_hash(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
