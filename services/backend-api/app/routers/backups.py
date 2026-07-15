"""备份总账、运行状态上报与管理员查询。"""

from __future__ import annotations

import hmac
import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.core import _conn, require_admin


router = APIRouter()


class BackupRunReport(BaseModel):
    policy_code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    run_id: str = Field(min_length=1, max_length=160)
    status: str = Field(pattern="^(success|partial|failed)$")
    source_device: str = Field(min_length=1, max_length=80)
    started_at: datetime
    finished_at: datetime
    snapshot_id: str | None = Field(default=None, max_length=200)
    data_bytes: int | None = Field(default=None, ge=0)
    file_count: int | None = Field(default=None, ge=0)
    destinations: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    detail: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = Field(default=None, max_length=2000)


class BackupRestoreCheckReport(BaseModel):
    policy_code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    status: str = Field(pattern="^(success|failed)$")
    checked_at: datetime
    source_snapshot_id: str | None = Field(default=None, max_length=200)
    target_device: str = Field(min_length=1, max_length=80)
    detail: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = Field(default=None, max_length=2000)


def _require_backup_report_token(x_backup_report_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("BACKUP_REPORT_TOKEN", "").strip()
    supplied = (x_backup_report_token or "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="invalid backup report token")


@router.post("/v1/internal/backups/report")
def report_backup_run(
    body: BackupRunReport,
    _: None = Depends(_require_backup_report_token),
) -> dict[str, Any]:
    if body.finished_at < body.started_at:
        raise HTTPException(status_code=422, detail="finished_at must not precede started_at")
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM backup_policies WHERE code = %s", (body.policy_code,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="backup policy not found")
                cur.execute(
                    """
                    INSERT INTO backup_runs(
                        policy_code, run_id, status, source_device, started_at, finished_at,
                        snapshot_id, data_bytes, file_count, destinations_json, detail_json, error_message
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (policy_code, run_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        source_device = EXCLUDED.source_device,
                        started_at = EXCLUDED.started_at,
                        finished_at = EXCLUDED.finished_at,
                        snapshot_id = EXCLUDED.snapshot_id,
                        data_bytes = EXCLUDED.data_bytes,
                        file_count = EXCLUDED.file_count,
                        destinations_json = EXCLUDED.destinations_json,
                        detail_json = EXCLUDED.detail_json,
                        error_message = EXCLUDED.error_message
                    RETURNING id
                    """,
                    (
                        body.policy_code, body.run_id, body.status, body.source_device,
                        body.started_at, body.finished_at, body.snapshot_id, body.data_bytes,
                        body.file_count, Jsonb(body.destinations), Jsonb(body.detail), body.error_message,
                    ),
                )
                row_id = int(cur.fetchone()[0])
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"backup report write failed: {type(exc).__name__}") from exc
    return {"ok": True, "id": row_id, "policy_code": body.policy_code, "run_id": body.run_id}


@router.post("/v1/internal/backups/restore-check")
def report_backup_restore_check(
    body: BackupRestoreCheckReport,
    _: None = Depends(_require_backup_report_token),
) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM backup_policies WHERE code = %s", (body.policy_code,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="backup policy not found")
                cur.execute(
                    """
                    INSERT INTO backup_restore_checks(
                      policy_code, status, checked_at, source_snapshot_id,
                      target_device, detail_json, error_message
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (body.policy_code, body.status, body.checked_at, body.source_snapshot_id,
                     body.target_device, Jsonb(body.detail), body.error_message),
                )
                row_id = int(cur.fetchone()[0])
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"restore check report write failed: {type(exc).__name__}") from exc
    return {"ok": True, "id": row_id, "policy_code": body.policy_code}


def _as_iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _json_value(value: Any, fallback: Any) -> Any:
    return value if isinstance(value, type(fallback)) else fallback


def _classify_policy(item: dict[str, Any], now: datetime | None = None) -> str:
    lifecycle = str(item.get("lifecycle_status") or "active")
    if lifecycle != "active":
        return lifecycle
    latest = item.get("latest_run") if isinstance(item.get("latest_run"), dict) else None
    if not latest:
        return "unknown"
    if latest.get("status") == "failed":
        return "failed"
    if latest.get("status") == "partial":
        return "warning"
    destinations = latest.get("destinations") if isinstance(latest.get("destinations"), list) else []
    if any(str(dest.get("status") or "").lower() not in {"ok", "success"} for dest in destinations if isinstance(dest, dict)):
        return "warning"
    finished_at = latest.get("finished_at_raw")
    if isinstance(finished_at, datetime):
        point = finished_at if finished_at.tzinfo else finished_at.replace(tzinfo=timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - point.astimezone(timezone.utc)).total_seconds()
        failure_after = item.get("failure_after_seconds")
        warning_after = item.get("warning_after_seconds")
        if failure_after and age > int(failure_after):
            return "failed"
        if warning_after and age > int(warning_after):
            return "warning"
    return "ok"


def backup_policies_from_db() -> list[dict[str, Any]]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.code, p.name, p.purpose, p.asset, p.source_device, p.method,
                       p.schedule_label, p.expected_interval_seconds, p.warning_after_seconds,
                       p.failure_after_seconds, p.retention_policy, p.lifecycle_status,
                       p.monitoring_required, p.detail_json,
                       r.id, r.run_id, r.status, r.source_device, r.started_at, r.finished_at,
                       r.snapshot_id, r.data_bytes, r.file_count, r.destinations_json,
                       r.detail_json, r.error_message,
                       c.status, c.checked_at, c.source_snapshot_id, c.target_device,
                       c.detail_json, c.error_message
                FROM backup_policies p
                LEFT JOIN LATERAL (
                    SELECT * FROM backup_runs br
                    WHERE br.policy_code = p.code
                    ORDER BY br.finished_at DESC, br.id DESC LIMIT 1
                ) r ON TRUE
                LEFT JOIN LATERAL (
                    SELECT * FROM backup_restore_checks bc
                    WHERE bc.policy_code = p.code
                    ORDER BY bc.checked_at DESC, bc.id DESC LIMIT 1
                ) c ON TRUE
                ORDER BY p.sort_order, p.code
                """
            )
            rows = cur.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        latest_run = None
        if row[14] is not None:
            latest_run = {
                "id": row[14], "run_id": row[15], "status": row[16], "source_device": row[17],
                "started_at": _as_iso(row[18]), "finished_at": _as_iso(row[19]),
                "finished_at_raw": row[19], "snapshot_id": row[20], "data_bytes": row[21],
                "file_count": row[22], "destinations": _json_value(row[23], []),
                "detail": _json_value(row[24], {}), "error_message": row[25],
            }
        restore_check = None
        if row[26] is not None:
            restore_check = {
                "status": row[26], "checked_at": _as_iso(row[27]), "source_snapshot_id": row[28],
                "target_device": row[29], "detail": _json_value(row[30], {}), "error_message": row[31],
            }
        item = {
            "code": row[0], "name": row[1], "purpose": row[2], "asset": row[3],
            "source_device": row[4], "method": row[5], "schedule_label": row[6],
            "expected_interval_seconds": row[7], "warning_after_seconds": row[8],
            "failure_after_seconds": row[9], "retention_policy": row[10],
            "lifecycle_status": row[11], "monitoring_required": bool(row[12]),
            "detail": _json_value(row[13], {}), "latest_run": latest_run,
            "latest_restore_check": restore_check,
        }
        item["status"] = _classify_policy(item)
        if latest_run:
            latest_run.pop("finished_at_raw", None)
        items.append(item)
    return items


def backup_summary_from_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    monitored = [item for item in items if item.get("monitoring_required")]
    for item in items:
        key = str(item.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    monitored_statuses = {str(item.get("status") or "unknown") for item in monitored}
    if "failed" in monitored_statuses:
        overall = "failed"
    elif monitored_statuses & {"warning", "unknown"}:
        overall = "warning"
    else:
        overall = "ok"
    successes = [
        item["latest_run"]["finished_at"] for item in items
        if item.get("latest_run") and item["latest_run"].get("status") == "success"
    ]
    return {
        "status": overall,
        "total": len(items),
        "monitored": len(monitored),
        "ok": counts.get("ok", 0),
        "warning": counts.get("warning", 0),
        "failed": counts.get("failed", 0),
        "unknown": counts.get("unknown", 0),
        "planned": counts.get("planned", 0),
        "covered": counts.get("covered", 0),
        "passive": counts.get("passive", 0),
        "excluded": counts.get("excluded", 0),
        "last_success_at": max(successes) if successes else None,
    }


def backup_summary_from_db() -> dict[str, Any]:
    try:
        return backup_summary_from_items(backup_policies_from_db())
    except Exception as exc:
        return {
            "status": "unknown", "total": 0, "monitored": 0, "ok": 0, "warning": 0,
            "failed": 0, "unknown": 0, "planned": 0, "covered": 0, "passive": 0,
            "excluded": 0, "last_success_at": None, "error": type(exc).__name__,
        }


@router.get("/v1/ops/backups")
def ops_backups(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        items = backup_policies_from_db()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"backup catalog unavailable: {type(exc).__name__}") from exc
    return {"summary": backup_summary_from_items(items), "items": items}


@router.get("/v1/ops/backups/{policy_code}/runs")
def ops_backup_runs(
    policy_code: str,
    limit: int = Query(default=20, ge=1, le=100),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, run_id, status, source_device, started_at, finished_at, snapshot_id,
                       data_bytes, file_count, destinations_json, detail_json, error_message
                FROM backup_runs WHERE policy_code = %s
                ORDER BY finished_at DESC, id DESC LIMIT %s
                """,
                (policy_code, limit),
            )
            rows = cur.fetchall()
    return {
        "items": [
            {
                "id": row[0], "run_id": row[1], "status": row[2], "source_device": row[3],
                "started_at": _as_iso(row[4]), "finished_at": _as_iso(row[5]),
                "snapshot_id": row[6], "data_bytes": row[7], "file_count": row[8],
                "destinations": _json_value(row[9], []), "detail": _json_value(row[10], {}),
                "error_message": row[11],
            }
            for row in rows
        ]
    }
