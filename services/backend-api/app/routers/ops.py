"""健康检查与运维域：healthz/readyz、T+同步运行与时间线、对账复核、主机状态探测、微信登录二维码、企微B消息采集。"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import psycopg
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from app.integrations.events import build_ops_attention_items
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from psycopg.types.json import Jsonb
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel, Field

from app.core import _audit, _conn, _db_ping, require_admin
from app.recipes.active_bom import copy_latest_bom_source, export_active_bom_rows
from app.routers.couple import _upload_disk_usage
from app.routers.exports import _match_export_files_to_runs, _tplus_export_dir
from app.routers.backups import backup_summary_from_db


router = APIRouter()
LOGGER = logging.getLogger(__name__)

class ReconciliationActionRequest(BaseModel):
    action: str = Field(pattern="^(use_current|use_previous|use_full|use_incremental|ignore)$")
    note: str | None = Field(default=None, max_length=500)


@router.get("/healthz")
def healthz() -> dict[str, object]:
    db_ok, db_message = _db_ping()
    upload_dir = os.getenv("LOCAL_UPLOAD_DIR", "/tmp/aliecs-uploads")
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "backend-api",
        "database": {"ok": db_ok, "message": db_message},
        "upload_disk": _upload_disk_usage(upload_dir),
    }


_SYNC_CONFIG_DEFAULTS = {"enabled": True, "interval_seconds": 86400, "anchor_time": ""}


def _read_sync_config_row(provider: str = "chanjet") -> dict[str, Any]:
    """读取定时同步配置行；DB 不可用 / 表或行不存在一律回退默认（不报错）。"""
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT enabled, interval_seconds, anchor_time, updated_at, updated_by "
                    "FROM integration_sync_config WHERE provider = %s",
                    (provider,),
                )
                row = cur.fetchone()
                if row:
                    return {"enabled": bool(row[0]), "interval_seconds": int(row[1]),
                            "anchor_time": str(row[2] or ""),
                            "updated_at": str(row[3]) if row[3] else None, "updated_by": row[4]}
    except Exception:
        pass
    return {**_SYNC_CONFIG_DEFAULTS, "updated_at": None, "updated_by": None}


def _sync_config_response(row: dict[str, Any]) -> dict[str, Any]:
    seconds = int(row.get("interval_seconds") or 86400)
    return {
        "enabled": bool(row.get("enabled", True)),
        "interval_seconds": seconds,
        "interval_hours": round(seconds / 3600, 4),
        "anchor_time": str(row.get("anchor_time") or ""),
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by"),
    }


class SyncConfigUpdate(BaseModel):
    enabled: bool
    interval_hours: float = Field(ge=1, le=168)  # 下限 1h（防误填打爆机器）、上限 7d
    # 北京时间 HH:MM，空=不锚定（保持"跑完睡一个周期"的旧行为）
    anchor_time: str = Field(default="", pattern=r"^$|^([01]\d|2[0-3]):[0-5]\d$")


@router.get("/v1/ops/tplus/sync-config")
def ops_tplus_sync_config_get(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """定时同步开关与间隔。worker 每轮热读同一张表。"""
    return _sync_config_response(_read_sync_config_row())


@router.put("/v1/ops/tplus/sync-config")
def ops_tplus_sync_config_put(
    body: SyncConfigUpdate, user: dict[str, Any] = Depends(require_admin)
) -> dict[str, Any]:
    interval_seconds = int(round(body.interval_hours * 3600))
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO integration_sync_config(provider, enabled, interval_seconds, anchor_time, updated_at, updated_by)
                    VALUES ('chanjet', %s, %s, %s, NOW(), %s)
                    ON CONFLICT (provider) DO UPDATE
                    SET enabled = EXCLUDED.enabled,
                        interval_seconds = EXCLUDED.interval_seconds,
                        anchor_time = EXCLUDED.anchor_time,
                        updated_at = NOW(),
                        updated_by = EXCLUDED.updated_by
                    """,
                    (body.enabled, interval_seconds, body.anchor_time, str(user.get("sub") or "")),
                )
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存定时同步配置失败：{type(exc).__name__}") from exc
    return _sync_config_response(_read_sync_config_row())


_DOC_SYNC_CONFIG_DEFAULTS = {"enabled": True, "interval_seconds": 86400, "anchor_time": "", "pull_paused": False}


def _read_doc_sync_config_row() -> dict[str, Any]:
    """读取文档同步调度配置行；DB 不可用 / 表列或行不存在一律回退默认（不报错）。"""
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT enabled, interval_seconds, anchor_time, pull_paused, updated_at, updated_by "
                    "FROM integration_sync_config WHERE provider = 'doc_sync'"
                )
                row = cur.fetchone()
                if row:
                    return {
                        "enabled": bool(row[0]),
                        "interval_seconds": int(row[1]),
                        "anchor_time": str(row[2] or ""),
                        "pull_paused": bool(row[3]),
                        "updated_at": str(row[4]) if row[4] else None,
                        "updated_by": str(row[5] or ""),
                    }
    except Exception:
        pass
    return {**_DOC_SYNC_CONFIG_DEFAULTS, "updated_at": None, "updated_by": ""}


def _doc_sync_source_label(updated_by: str) -> str:
    if updated_by == "feishu-config-table":
        return "飞书配置表"
    if not updated_by:
        return "默认"
    return "手动"


def _doc_sync_config_response(row: dict[str, Any]) -> dict[str, Any]:
    seconds = int(row.get("interval_seconds") or 86400)
    updated_by = str(row.get("updated_by") or "")
    return {
        "enabled": bool(row.get("enabled", True)),
        "interval_seconds": seconds,
        "interval_hours": round(seconds / 3600, 4),
        "anchor_time": str(row.get("anchor_time") or ""),
        "pull_paused": bool(row.get("pull_paused", False)),
        "updated_at": row.get("updated_at"),
        "updated_by": updated_by,
        "source": _doc_sync_source_label(updated_by),
    }


class DocSyncConfigUpdate(BaseModel):
    enabled: bool
    interval_hours: float = Field(ge=1, le=168)
    anchor_time: str = Field(default="", pattern=r"^$|^([01]\d|2[0-3]):[0-5]\d$")  # 北京时间 HH:MM，空=不锚定
    pull_paused: bool = False


@router.get("/v1/ops/doc-sync/sync-config")
def ops_doc_sync_config_get(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """文档同步（企微+飞书）定时开关/周期/起点时间。worker 每轮热读同一张表；
    updated_by=feishu-config-table 表示当前生效值来自飞书「配置表」。"""
    return _doc_sync_config_response(_read_doc_sync_config_row())


@router.put("/v1/ops/doc-sync/sync-config")
def ops_doc_sync_config_put(
    body: DocSyncConfigUpdate, user: dict[str, Any] = Depends(require_admin)
) -> dict[str, Any]:
    """管理页应急覆盖：写全字段（含 pull_paused）。pull_paused=true 时 worker 停止表格拉取，手动值不会被覆盖。"""
    interval_seconds = int(round(body.interval_hours * 3600))
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO integration_sync_config(provider, enabled, interval_seconds, anchor_time, pull_paused, updated_at, updated_by)
                    VALUES ('doc_sync', %s, %s, %s, %s, NOW(), %s)
                    ON CONFLICT (provider) DO UPDATE
                    SET enabled = EXCLUDED.enabled,
                        interval_seconds = EXCLUDED.interval_seconds,
                        anchor_time = EXCLUDED.anchor_time,
                        pull_paused = EXCLUDED.pull_paused,
                        updated_at = NOW(),
                        updated_by = EXCLUDED.updated_by
                    """,
                    (body.enabled, interval_seconds, body.anchor_time, body.pull_paused, str(user.get("sub") or "")),
                )
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存文档同步配置失败：{type(exc).__name__}") from exc
    return _doc_sync_config_response(_read_doc_sync_config_row())


@router.get("/v1/ops/tplus/runs")
def ops_tplus_runs(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """全部 T+ 同步执行记录（含每小时 scheduled_full 与手动 bom），分页。
    数据源 integration_sync_runs，比 ops_status 里只取 10 条的 recent_requests 完整。"""
    items: list[dict[str, Any]] = []
    total = 0
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM integration_sync_runs WHERE provider = 'chanjet'")
                total = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT sr.id, sr.module, sr.mode, sr.status, sr.finished_at, sr.exit_code, sr.row_count,
                           req.id, req.reason_event_id
                    FROM integration_sync_runs sr
                    LEFT JOIN LATERAL (
                        SELECT id, reason_event_id
                        FROM integration_sync_requests
                        WHERE provider = 'chanjet' AND sync_run_id = sr.id
                        ORDER BY requested_at DESC NULLS LAST, id DESC
                        LIMIT 1
                    ) req ON TRUE
                    WHERE sr.provider = 'chanjet'
                    ORDER BY sr.finished_at DESC NULLS LAST, sr.id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                items = [
                    {
                        "id": row[0],
                        "module": row[1],
                        "mode": row[2],
                        "status": row[3],
                        "finished_at": str(row[4]) if row[4] else None,
                        "exit_code": row[5],
                        "row_count": row[6],
                        "request_id": row[7],
                        "reason_event_id": row[8],
                    }
                    for row in cur.fetchall()
                ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取 T+ 同步记录失败：{type(exc).__name__}") from exc
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/v1/ops/tplus/requests")
def ops_tplus_requests(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """全部 T+ 同步请求（畅捷通回调事件触发的 bom 同步），分页。reason_event_id=回调事件ID。"""
    items: list[dict[str, Any]] = []
    total = 0
    counts = {"pending": 0, "running": 0, "failed": 0}
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM integration_sync_requests WHERE provider = 'chanjet'")
                total = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT status, COUNT(*) FROM integration_sync_requests WHERE provider = 'chanjet' GROUP BY status"
                )
                status_map = {row[0]: int(row[1]) for row in cur.fetchall()}
                counts = {key: status_map.get(key, 0) for key in ("pending", "running", "failed")}
                cur.execute(
                    """
                    SELECT r.id, r.module, r.mode, r.status, r.requested_at, r.started_at,
                           r.finished_at, r.reason_event_id, r.target_json, r.sync_run_id,
                           r.error_json, sr.detail_json, sr.error_json, sr.row_count, sr.exit_code
                    FROM integration_sync_requests r
                    LEFT JOIN integration_sync_runs sr ON sr.id = r.sync_run_id
                    WHERE r.provider = 'chanjet'
                    ORDER BY r.requested_at DESC, r.id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                items = [
                    {
                        "id": row[0],
                        "module": row[1],
                        "mode": row[2],
                        "status": row[3],
                        "requested_at": str(row[4]) if row[4] else None,
                        "started_at": str(row[5]) if row[5] else None,
                        "finished_at": str(row[6]) if row[6] else None,
                        "reason_event_id": row[7],
                        "target_json": _json_value(row[8]),
                        "sync_run_id": row[9],
                        "request_error_json": _json_value(row[10]),
                        "detail_json": _json_value(row[11]),
                        "error_json": _json_value(row[12]),
                        "row_count": row[13],
                        "exit_code": row[14],
                    }
                    for row in cur.fetchall()
                ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取 T+ 同步请求失败：{type(exc).__name__}") from exc
    return {"items": items, "total": total, "limit": limit, "offset": offset, **counts}


@router.get("/v1/ops/tplus/timeline")
def ops_tplus_timeline(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """统一时间线：执行(run) + 无执行的孤儿请求，按时间倒序分页；附产出 Excel 与变化摘要。"""
    items: list[dict[str, Any]] = []
    total = 0
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT (SELECT COUNT(*) FROM integration_sync_runs WHERE provider='chanjet')
                         + (SELECT COUNT(*) FROM integration_sync_requests
                            WHERE provider='chanjet' AND sync_run_id IS NULL)
                    """
                )
                total = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT kind, id, module, mode, status, event_time, row_count, exit_code,
                           reason_event_id, request_id, detail_json, reconciliation_id
                    FROM (
                        SELECT 'run' AS kind, sr.id AS id, sr.module, sr.mode, sr.status,
                               sr.finished_at AS event_time, sr.row_count, sr.exit_code,
                               req.reason_event_id, req.id AS request_id, sr.detail_json,
                               rec.id AS reconciliation_id
                        FROM integration_sync_runs sr
                        LEFT JOIN LATERAL (
                            SELECT id, reason_event_id FROM integration_sync_requests
                            WHERE provider='chanjet' AND sync_run_id = sr.id
                            ORDER BY requested_at DESC NULLS LAST, id DESC LIMIT 1
                        ) req ON TRUE
                        LEFT JOIN integration_reconciliation_diffs rec
                            ON rec.provider='chanjet'
                            AND rec.full_snapshot_id = NULLIF(sr.detail_json->>'full_snapshot_id','')::bigint
                        WHERE sr.provider='chanjet'
                        UNION ALL
                        SELECT 'request' AS kind, r.id, r.module, r.mode, r.status,
                               r.requested_at AS event_time, NULL::int, NULL::int,
                               r.reason_event_id, r.id, r.error_json, NULL::bigint
                        FROM integration_sync_requests r
                        WHERE r.provider='chanjet' AND r.sync_run_id IS NULL
                    ) merged
                    ORDER BY event_time DESC NULLS LAST, kind, id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取 T+ 时间线失败：{type(exc).__name__}") from exc

    run_rows = [(r[1], r[5]) for r in rows if r[0] == "run"]
    export_dir = _tplus_export_dir()
    disk_files = [p.name for p in export_dir.glob("*.xlsx")] if export_dir.is_dir() else []
    fallback = _match_export_files_to_runs(run_rows, disk_files)
    existing = set(disk_files)

    for (kind, rid, module, mode, status, event_time, row_count, exit_code,
         reason_event_id, request_id, detail, reconciliation_id) in rows:
        detail = _json_value(detail) or {}
        diff_summary = detail.get("diff_summary")
        row: dict[str, Any] = {
            "kind": kind,
            "number": f"#{rid}" if kind == "run" else f"请求·R{rid}",
            "id": rid,
            "module": module,
            "mode": mode,
            "status": status,
            "event_time": str(event_time) if event_time else None,
            "row_count": row_count,
            "exit_code": exit_code,
            "reason_event_id": reason_event_id,
            "request_id": request_id,
            "diff_summary": diff_summary,
            "needs_review": bool((diff_summary or {}).get("needs_review")),
            "reconciliation_id": reconciliation_id,
            "export_files": [],
        }
        if kind == "run":
            names = list(detail.get("export_files") or []) or fallback.get(rid, [])
            row["export_files"] = [
                {"name": name,
                 "download_url": f"/v1/exports/tplus/{name}" if name in existing else None,
                 "pruned": name not in existing}
                for name in names
            ]
        items.append(row)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/v1/ops/status")
def ops_status(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    db_ok, db_message = _db_ping()
    status: dict[str, Any] = {
        "status": "ok" if db_ok else "degraded",
        "service": "backend-api",
        "database": {"ok": db_ok, "message": db_message},
        "system": _system_status(),
        "tplus": _tplus_status_from_db() if db_ok else _empty_tplus_status(),
        "reconciliation": _reconciliation_status_from_db() if db_ok else {"needs_review": 0, "recent": []},
        "backups": backup_summary_from_db() if db_ok else {"status": "unknown", "total": 0, "monitored": 0},
        "hosts": _configured_host_statuses(),
        "chanjet_token": _chanjet_token_status(),
    }
    status["attention_items"] = build_ops_attention_items(status)
    if status["attention_items"]:
        status["status"] = "degraded"
    return status


@router.get("/v1/ops/hosts")
def ops_hosts() -> dict[str, Any]:
    return {"items": _configured_host_statuses()}


@router.get("/v1/ops/hosts/{host_name}/refresh")
def ops_host_refresh(host_name: str) -> dict[str, Any]:
    for target in _ops_http_targets():
        if str(target.get("name") or target.get("url") or "target") == host_name:
            return _probe_http_target(target)
    raise HTTPException(status_code=404, detail="host target not found")


def _empty_tplus_status() -> dict[str, Any]:
    return {
        "pending_requests": 0,
        "running_requests": 0,
        "failed_requests": 0,
        "last_success_at": None,
        "last_run": None,
        "recent_requests": [],
    }


def _system_status() -> dict[str, Any]:
    disk = shutil.disk_usage("/")
    memory = _memory_status()
    result: dict[str, Any] = {
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_free": disk.free,
        "disk_percent": round(disk.used / disk.total * 100, 1) if disk.total else 0.0,
        **memory,
    }
    try:
        load1, load5, load15 = os.getloadavg()
        result["loadavg"] = [round(load1, 2), round(load5, 2), round(load15, 2)]
    except (AttributeError, OSError):
        result["loadavg"] = []
    return result


def _memory_status() -> dict[str, Any]:
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return {"memory_total": 0, "memory_available": 0, "memory_percent": 0.0}
    values: dict[str, int] = {}
    for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        parts = raw.strip().split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used = max(total - available, 0)
    return {
        "memory_total": total,
        "memory_available": available,
        "memory_percent": round(used / total * 100, 1) if total else 0.0,
    }


def _tplus_status_from_db() -> dict[str, Any]:
    status = _empty_tplus_status()
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, COUNT(*)
                    FROM integration_sync_requests
                    WHERE provider = 'chanjet' AND module = 'bom'
                    GROUP BY status
                    """
                )
                for row_status, count in cur.fetchall():
                    if row_status == "pending":
                        status["pending_requests"] = int(count)
                    elif row_status == "running":
                        status["running_requests"] = int(count)
                    elif row_status == "failed":
                        status["failed_requests"] = int(count)
                cur.execute(
                    """
                    SELECT id, module, mode, status, started_at, finished_at, row_count, exit_code, detail_json
                    FROM integration_sync_runs
                    WHERE provider = 'chanjet'
                    ORDER BY started_at DESC, id DESC
                    LIMIT 1
                    """
                )
                last_run = cur.fetchone()
                if last_run:
                    status["last_run"] = _sync_run_to_dict(last_run)
                cur.execute(
                    """
                    SELECT finished_at
                    FROM integration_sync_runs
                    WHERE provider = 'chanjet' AND status = 'success'
                    ORDER BY finished_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """
                )
                last_success = cur.fetchone()
                if last_success and last_success[0]:
                    status["last_success_at"] = str(last_success[0])
                cur.execute(
                    """
                    SELECT r.id, r.module, r.mode, r.status, r.requested_at, r.started_at,
                           r.finished_at, r.reason_event_id, r.target_json, r.sync_run_id,
                           r.error_json, sr.detail_json, sr.error_json, sr.row_count, sr.exit_code
                    FROM integration_sync_requests r
                    LEFT JOIN integration_sync_runs sr ON sr.id = r.sync_run_id
                    WHERE r.provider = 'chanjet'
                    ORDER BY r.requested_at DESC, r.id DESC
                    LIMIT 10
                    """
                )
                status["recent_requests"] = [
                    {
                        "id": row[0],
                        "module": row[1],
                        "mode": row[2],
                        "status": row[3],
                        "requested_at": str(row[4]) if row[4] else None,
                        "started_at": str(row[5]) if row[5] else None,
                        "finished_at": str(row[6]) if row[6] else None,
                        "reason_event_id": row[7],
                        "target_json": _json_value(row[8]),
                        "sync_run_id": row[9],
                        "request_error_json": _json_value(row[10]),
                        "detail_json": _json_value(row[11]),
                        "error_json": _json_value(row[12]),
                        "row_count": row[13],
                        "exit_code": row[14],
                    }
                    for row in cur.fetchall()
                ]
    except Exception as exc:
        status["error"] = str(exc)
    return status


def _reconciliation_status_from_db() -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM integration_reconciliation_diffs
                    WHERE status = 'needs_review'
                    """
                )
                needs_review = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT id, provider, module, severity, summary, created_at
                    FROM integration_reconciliation_diffs
                    WHERE status = 'needs_review'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 10
                    """
                )
                recent = [
                    {
                        "id": row[0],
                        "provider": row[1],
                        "module": row[2],
                        "severity": row[3],
                        "summary": row[4],
                        "created_at": str(row[5]) if row[5] else None,
                    }
                    for row in cur.fetchall()
                ]
        return {"needs_review": needs_review, "recent": recent}
    except Exception as exc:
        return {"needs_review": 0, "recent": [], "error": str(exc)}


def _json_value(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    if hasattr(value, "obj"):
        return getattr(value, "obj")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {"raw": value}
    return value


def _reconciliation_diff_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "provider": row[1],
        "module": row[2],
        "status": row[3],
        "severity": row[4],
        "summary": row[5],
        "diff_json": _json_value(row[6]),
        "full_snapshot_id": row[7],
        "incremental_snapshot_id": row[8],
        "created_at": str(row[9]) if row[9] else None,
        "reviewed_at": str(row[10]) if row[10] else None,
        "reviewed_by": row[11],
        "resolution": _json_value(row[12]),
    }


@router.get("/v1/ops/reconciliation/{diff_id}")
def ops_reconciliation_detail(diff_id: int, _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, provider, module, status, severity, summary, diff_json,
                       full_snapshot_id, incremental_snapshot_id, created_at,
                       reviewed_at, reviewed_by, resolution_json
                FROM integration_reconciliation_diffs
                WHERE id = %s
                """,
                (diff_id,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="reconciliation diff not found")
    return _reconciliation_diff_to_dict(row)


@router.post("/v1/ops/reconciliation/{diff_id}/actions")
def ops_reconciliation_action(
    diff_id: int,
    body: ReconciliationActionRequest,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    next_status = "ignored" if body.action == "ignore" else "resolved"
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, provider, module, status, severity, summary, diff_json,
                       full_snapshot_id, incremental_snapshot_id, created_at,
                       reviewed_at, reviewed_by, resolution_json
                FROM integration_reconciliation_diffs
                WHERE id = %s
                FOR UPDATE
                """,
                (diff_id,),
            )
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="reconciliation diff not found")

            existing_diff = _reconciliation_diff_to_dict(existing)
            selected_snapshot_id = _selected_reconciliation_snapshot(existing_diff, body.action)
            resolution = {
                "action": body.action,
                "note": body.note or "",
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }
            if next_status == "resolved":
                if selected_snapshot_id is None:
                    raise HTTPException(status_code=400, detail="selected snapshot not found")
                activation = _activate_bom_snapshot(conn, selected_snapshot_id, allow_latest_fallback=body.action != "use_previous")
                resolution.update(activation)
                resolution["selected_snapshot_id"] = selected_snapshot_id

            cur.execute(
                """
                UPDATE integration_reconciliation_diffs
                SET status = %s,
                    resolution_json = %s,
                    reviewed_at = NOW(),
                    reviewed_by = %s
                WHERE id = %s
                RETURNING id, provider, module, status, severity, summary, diff_json,
                          full_snapshot_id, incremental_snapshot_id, created_at,
                          reviewed_at, reviewed_by, resolution_json
                """,
                (next_status, Jsonb(resolution), user.get("sub", ""), diff_id),
            )
            row = cur.fetchone()
            if next_status == "resolved":
                cur.execute(
                    """
                    UPDATE integration_reconciliation_diffs
                    SET status = 'superseded',
                        resolution_json = %s,
                        reviewed_at = NOW(),
                        reviewed_by = %s
                    WHERE provider = %s
                      AND module = %s
                      AND status = 'needs_review'
                      AND id < %s
                    """,
                    (
                        Jsonb(
                            {
                                "action": "superseded_by_newer_resolution",
                                "superseded_by_diff_id": diff_id,
                                "selected_snapshot_id": selected_snapshot_id,
                                "active_export_name": resolution.get("active_export_name"),
                                "resolved_at": resolution["resolved_at"],
                            }
                        ),
                        user.get("sub", ""),
                        existing_diff["provider"],
                        existing_diff["module"],
                        diff_id,
                    ),
                )
        conn.commit()
    _audit(user.get("sub"), "ops.reconciliation.resolve", "integration_reconciliation_diffs", str(diff_id), resolution)
    return _reconciliation_diff_to_dict(row)


def _selected_reconciliation_snapshot(diff: dict[str, Any], action: str) -> int | None:
    diff_json = diff.get("diff_json") if isinstance(diff.get("diff_json"), dict) else {}
    if action == "ignore":
        return None
    if action in {"use_current", "use_incremental"}:
        return _int_or_none(diff.get("incremental_snapshot_id")) or _int_or_none(diff_json.get("current_snapshot_id")) or _int_or_none(diff.get("full_snapshot_id"))
    if action == "use_previous":
        return _int_or_none(diff_json.get("previous_snapshot_id"))
    if action == "use_full":
        return _int_or_none(diff.get("full_snapshot_id")) or _int_or_none(diff_json.get("current_snapshot_id"))
    return None


def _activate_bom_snapshot(conn: psycopg.Connection, snapshot_id: int, *, allow_latest_fallback: bool = True) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_json
            FROM integration_sync_snapshots
            WHERE id = %s
            """,
            (snapshot_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="selected snapshot not found")
    source = _json_value(row[0])
    records = source.get("records") if isinstance(source.get("records"), list) else []
    if records:
        return export_active_bom_rows(records)
    if not allow_latest_fallback:
        raise HTTPException(status_code=409, detail="selected snapshot has no stored BOM records")
    return copy_latest_bom_source()


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _sync_run_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "module": row[1],
        "mode": row[2],
        "status": row[3],
        "started_at": str(row[4]) if row[4] else None,
        "finished_at": str(row[5]) if row[5] else None,
        "row_count": row[6],
        "exit_code": row[7],
        "detail_json": _json_value(row[8]),
    }


def _configured_host_statuses() -> list[dict[str, Any]]:
    return [_probe_http_target(item) for item in _ops_http_targets()]


def _ops_http_targets() -> list[dict[str, Any]]:
    raw = os.getenv("OPS_HEALTH_HTTP_TARGETS_JSON", "").strip()
    if not raw:
        targets = [
            {
                "name": "AliECS Backend API",
                "url": os.getenv("OPS_HEALTH_BACKEND_URL", "").strip(),
                "description": "公网反代后的 AliECS 后端健康检查。",
                "timeout": 3,
            },
            {
                "name": "AliECS Public Web",
                "url": os.getenv("OPS_HEALTH_PUBLIC_WEB_URL", "").strip(),
                "description": "公网首页入口。",
                "timeout": 3,
            },
            {
                "name": "WebDock API",
                "url": os.getenv("OPS_HEALTH_WEBDOCK_API_URL", "http://host.docker.internal:11800/healthz"),
                "description": "旧电脑 WebDock API，经服务器 SSH 隧道 11800 端口探测。",
                "timeout": 3,
            },
        ]
        novnc_url = os.getenv("OPS_HEALTH_WEBDOCK_NOVNC_URL", "").strip()
        if novnc_url:
            targets.append(
                {
                    "name": "WebDock noVNC",
                    "url": novnc_url,
                    "description": "旧电脑 noVNC 页面，需显式配置可由 backend-api 访问的地址。",
                    "timeout": 3,
                }
            )
        # Drop targets without an explicit URL (e.g. backend/public-web before
        # OPS_HEALTH_*_URL is configured) so the health page doesn't render
        # blank "url is empty" rows.
        return [t for t in targets if str(t.get("url") or "").strip()]
    try:
        targets = json.loads(raw)
    except Exception as exc:
        return [{"name": "OPS_HEALTH_HTTP_TARGETS_JSON", "url": "", "description": f"invalid json: {exc}"}]
    if not isinstance(targets, list):
        return [{"name": "OPS_HEALTH_HTTP_TARGETS_JSON", "url": "", "description": "must be a list"}]
    return [item for item in targets if isinstance(item, dict)]


def _probe_http_target(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or item.get("url") or "target")
    url = str(item.get("url") or "")
    description = str(item.get("description") or "")
    checked_at = datetime.now(timezone.utc).isoformat()
    timeout = float(item.get("timeout") or 2)
    if not url:
        return {"name": name, "url": url, "description": description, "ok": False, "message": "url is empty", "last_checked_at": checked_at}
    started = time.perf_counter()
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = int(getattr(response, "status", 0) or 0)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "name": name,
            "url": url,
            "description": description,
            "ok": 200 <= status_code < 500,
            "status_code": status_code,
            "latency_ms": elapsed_ms,
            "last_checked_at": checked_at,
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "name": name,
            "url": url,
            "description": description,
            "ok": False,
            "message": str(exc),
            "latency_ms": elapsed_ms,
            "last_checked_at": checked_at,
        }


def _wechat_login_qr_from_gateway() -> dict[str, Any] | None:
    url = os.getenv("OPENCLAW_WECHAT_LOGIN_QR_URL", "").strip()
    if not url:
        return None
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"openclaw qr gateway failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="openclaw qr gateway returned non-object json")
    if not (payload.get("qr_image_base64") or payload.get("qr_url")):
        raise HTTPException(status_code=502, detail="openclaw qr gateway response missing qr_image_base64/qr_url")
    payload = dict(payload)
    payload.setdefault("source", "gateway")
    return payload


def _wechat_login_qr_from_file() -> dict[str, Any] | None:
    raw_path = os.getenv("OPENCLAW_WECHAT_LOGIN_QR_FILE", "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="wechat login qr file not found")
    data = path.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return {"qr_image_base64": "data:image/png;base64," + base64.b64encode(data).decode("ascii"), "source": "file"}
    text = data.decode("utf-8", errors="replace").strip()
    if text.startswith(("http://", "https://")):
        return {"qr_url": text, "source": "file"}
    if text.startswith("data:image/"):
        return {"qr_image_base64": text, "source": "file"}
    return {"qr_image_base64": "data:image/png;base64," + base64.b64encode(data).decode("ascii"), "source": "file"}


@router.get("/v1/ops/wechat/login-qr")
def ops_wechat_login_qr(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    payload = _wechat_login_qr_from_gateway() or _wechat_login_qr_from_file()
    if payload:
        payload.setdefault("expires_at", None)
        payload.setdefault("message", "等待新用户扫码加入微信 clawbot。")
        return payload
    raise HTTPException(
        status_code=503,
        detail="未配置稳定二维码来源。请在 OpenClaw 主机运行 openclaw channels login --channel openclaw-weixin，或配置 OPENCLAW_WECHAT_LOGIN_QR_URL/FILE。",
    )


def _extract_wecom_b_message(payload: dict[str, Any]) -> dict[str, Any]:
    body = payload.get("body") if isinstance(payload.get("body"), dict) else payload
    sender = body.get("from") if isinstance(body.get("from"), dict) else {}
    msg_type = str(body.get("msgtype") or body.get("msg_type") or "")
    content = ""
    if msg_type == "text" and isinstance(body.get("text"), dict):
        content = str(body["text"].get("content") or "")
    elif msg_type and isinstance(body.get(msg_type), dict):
        content = json.dumps(body[msg_type], ensure_ascii=False, sort_keys=True)
    msg_id = str(body.get("msgid") or body.get("msg_id") or payload.get("msgid") or "").strip()
    if not msg_id:
        raise HTTPException(status_code=400, detail="missing msgid")
    return {
        "msg_id": msg_id,
        "bot_id": str(body.get("aibotid") or body.get("bot_id") or ""),
        "chat_id": str(body.get("chatid") or body.get("chat_id") or ""),
        "chat_type": str(body.get("chattype") or body.get("chat_type") or ""),
        "sender_id": str(sender.get("userid") or sender.get("user_id") or body.get("from_user_id") or ""),
        "msg_type": msg_type,
        "content": content,
    }


@router.post("/v1/webhooks/wecom-b/messages")
def wecom_b_capture_message(
    payload: dict[str, Any],
    x_wecom_capture_token: str | None = Header(default=None),
) -> dict[str, str]:
    # Fail-closed: this endpoint writes to wecom_b_messages, so an unset token must
    # NOT mean "accept anything" — that would be an unauthenticated public DB-write.
    expected = os.getenv("WECOM_B_CAPTURE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="capture endpoint disabled: WECOM_B_CAPTURE_TOKEN not set")
    if not hmac.compare_digest(x_wecom_capture_token or "", expected):
        raise HTTPException(status_code=403, detail="invalid capture token")
    message = _extract_wecom_b_message(payload)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wecom_b_messages(
                    msg_id, bot_id, chat_id, chat_type, sender_id, msg_type, content, raw_json, received_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(msg_id) DO NOTHING
                """,
                (
                    message["msg_id"],
                    message["bot_id"],
                    message["chat_id"],
                    message["chat_type"],
                    message["sender_id"],
                    message["msg_type"],
                    message["content"],
                    Jsonb(payload),
                ),
            )
        conn.commit()
    return {"status": "received", "msg_id": message["msg_id"]}


@router.get("/readyz")
def readyz() -> dict[str, object]:
    db_ok, db_message = _db_ping()
    if db_ok:
        return {"status": "ready"}
    return {"status": "not-ready", "reason": db_message}


@router.get("/v1/ping")
def ping() -> dict[str, str]:
    return {"message": "pong"}


# ---------- T+ openToken 有效期监控 ----------
# openToken 有效期只有 6 天，全靠畅捷通每约 10 分钟推送 appTicket 到
# /v1/webhooks/chanjet 续期，服务端无法主动申请。链路一断（迁移、停机、消息地址
# 被平台标记「不再发送」）就静默失效，整条 T+ 同步与 BOM builder 一起挂。
# 正常时剩余始终贴近 6 天；掉到阈值以下即说明续期链路已断。详见
# docs/runbooks/tplus.md 的「openToken 续期链路」。

CHANJET_TOKEN_ALERT_THRESHOLD_SECONDS = 4 * 86400
CHANJET_TOKEN_ALERT_INTERVAL_SECONDS = 86400
CHANJET_ALERT_FEISHU_RECEIVE_ID = "oc_84d1130542509e374f7ea20c13d11ca4"


def _jwt_expiry(token: str) -> datetime | None:
    """取 JWT payload 里的 exp。非 JWT 或缺 exp 返回 None。"""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc)
    except Exception:
        return None


def _chanjet_token_status() -> dict[str, Any]:
    path = os.getenv("CHANJET_OPEN_TOKEN_FILE", "").strip()
    if not path:
        return {"configured": False, "ok": True, "message": "未配置 CHANJET_OPEN_TOKEN_FILE"}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        return {"configured": True, "ok": False, "message": f"读取 token 文件失败：{type(exc).__name__}"}
    if not token:
        return {"configured": True, "ok": False, "message": "token 文件为空"}
    expires_at = _jwt_expiry(token)
    if expires_at is None:
        return {"configured": True, "ok": False, "message": "token 解析不出有效期"}
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
    expired = remaining <= 0
    return {
        "configured": True,
        "ok": remaining >= CHANJET_TOKEN_ALERT_THRESHOLD_SECONDS,
        "expired": expired,
        "expires_at": expires_at.isoformat(),
        "remaining_hours": round(remaining / 3600, 1),
        "message": (
            "openToken 已失效" if expired
            else f"剩余 {round(remaining / 3600, 1)} 小时"
        ),
    }


def _chanjet_token_alert_text(status: dict[str, Any]) -> str:
    head = "⛔ T+ openToken 已失效" if status.get("expired") else "⚠️ T+ openToken 即将失效"
    lines = [head, status.get("message", "")]
    if status.get("expires_at"):
        lines.append(f"到期时间：{status['expires_at']}")
    lines += [
        "",
        "续期靠畅捷通推送 appTicket，链路断了不会自愈。",
        "处置：畅捷通开放平台 →「消息配置」→ 若「当前平台消息发送状态」显示"
        "「不再发送」，点「重置消息地址状态并发送AppTicket」。",
        "详见 AliECS/docs/runbooks/tplus.md",
    ]
    return "\n".join(line for line in lines if line is not None)


def _send_chanjet_token_alert(status: dict[str, Any]) -> bool:
    from app.routers.versions import send_feishu_text

    return send_feishu_text(
        os.getenv("OPS_ALERT_FEISHU_RECEIVE_ID", CHANJET_ALERT_FEISHU_RECEIVE_ID).strip(),
        _chanjet_token_alert_text(status),
        app_id=os.getenv("OPS_ALERT_FEISHU_APP_ID", os.getenv("VERSION_DIGEST_FEISHU_APP_ID", "")).strip(),
        app_secret=os.getenv("OPS_ALERT_FEISHU_APP_SECRET", os.getenv("VERSION_DIGEST_FEISHU_APP_SECRET", "")).strip(),
    )


def chanjet_token_alert_once() -> dict[str, Any]:
    """检查一次；异常就发飞书。不做去重——每天一次，坏着就每天提醒。"""
    status = _chanjet_token_status()
    if not status.get("configured") or status.get("ok"):
        return {"alerted": False, "status": status}
    return {"alerted": _send_chanjet_token_alert(status), "status": status}


def _chanjet_token_alert_loop() -> None:
    interval = _read_alert_interval()
    while True:
        try:
            chanjet_token_alert_once()
        except Exception:
            LOGGER.exception("chanjet openToken alert check failed")
        time.sleep(interval)


def _read_alert_interval() -> int:
    try:
        value = int(os.getenv("CHANJET_TOKEN_ALERT_INTERVAL_SECONDS", "").strip())
        return value if value > 0 else CHANJET_TOKEN_ALERT_INTERVAL_SECONDS
    except ValueError:
        return CHANJET_TOKEN_ALERT_INTERVAL_SECONDS


@router.on_event("startup")
def _start_chanjet_token_watcher() -> None:
    if os.getenv("CHANJET_TOKEN_ALERT_ENABLED", "1").strip() != "1":
        return
    threading.Thread(target=_chanjet_token_alert_loop, name="chanjet-token-watcher", daemon=True).start()
