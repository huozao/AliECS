from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from app import document_locator


WECOM_DOC_TYPES = {"smartsheet_doc", "registry_doc"}
WECOM_TABLE_TYPE = "smartsheet_sheet"
FEISHU_DOC_TYPE = "bitable_app"
FEISHU_TABLE_TYPE = "bitable_table"
_GROUPS = (
    ("tplus", "T+ ERP"),
    ("wecom_company_a", "企微A"),
    ("wecom_company_b", "企微B"),
    ("feishu", "飞书"),
)
_TPLUS_DEFAULTS = {"enabled": True, "interval_seconds": 86400, "anchor_time": ""}
_DOC_DEFAULTS = {
    "enabled": True,
    "interval_seconds": 86400,
    "anchor_time": "",
    "pull_paused": False,
}
PENDING_FULL_SYNC_SQL = """
SELECT id, status FROM integration_sync_requests
WHERE provider = 'chanjet' AND module = 'all' AND status IN ('pending', 'running')
ORDER BY id DESC LIMIT 1
"""
ENQUEUE_FULL_SYNC_SQL = """
INSERT INTO integration_sync_requests(provider, module, mode, target_json, priority, status, dedupe_key)
VALUES ('chanjet', 'all', 'manual_full', '{}'::jsonb, 50, 'pending', %s)
RETURNING id
"""


class InvalidSyncTarget(ValueError):
    pass


class SyncConfigUpdate(BaseModel):
    enabled: bool
    interval_hours: float = Field(ge=1, le=168)
    anchor_time: str = Field(default="", pattern=r"^$|^([01]\d|2[0-3]):[0-5]\d$")


class DocSyncConfigUpdate(SyncConfigUpdate):
    pull_paused: bool = False


def source_group(provider: str, env_profile: str) -> str:
    if provider == "chanjet":
        return "tplus"
    if provider == "feishu":
        return "feishu"
    return f"wecom_{str(env_profile or '').lower()}"


def valid_wecom_docid(value: str) -> bool:
    return document_locator.valid_wecom_docid(value)


def _doc_syncability(provider: str, source_type: str, external_doc_id: str, status: str = "active") -> tuple[bool, str]:
    if status != "active":
        return False, "同步源已停用"
    if provider == "wecom":
        if source_type not in WECOM_DOC_TYPES or not valid_wecom_docid(external_doc_id):
            return False, "缺少有效企微 docid"
        return True, ""
    if provider == "feishu":
        if source_type != FEISHU_DOC_TYPE or not external_doc_id:
            return False, "缺少有效飞书文档标识"
        return True, ""
    return False, "不支持的同步来源"


_ASSETS_SQL = """
SELECT
    d.provider,
    d.env_profile,
    d.external_doc_id,
    d.source_type,
    COALESCE(NULLIF(d.document_name, ''), NULLIF(d.source_name, ''), '未命名文档') AS document_name,
    d.source_name,
    d.id,
    COUNT(DISTINCT s.id) FILTER (
        WHERE s.status = 'active'
          AND ((s.provider = 'wecom' AND s.source_type = 'smartsheet_sheet')
            OR (s.provider = 'feishu' AND s.source_type = 'bitable_table'))
    ) AS sheet_count,
    COUNT(DISTINCT j.id) FILTER (WHERE j.enabled) AS job_count,
    MAX(s.last_sync_at) FILTER (WHERE s.status = 'active') AS last_sync_at
FROM external_sources d
LEFT JOIN external_sources s
  ON s.provider = d.provider
 AND s.env_profile = d.env_profile
 AND s.external_doc_id = d.external_doc_id
 AND s.external_sheet_id <> ''
LEFT JOIN sync_jobs j ON j.source_id = s.id AND j.kind = 'pull'
WHERE d.status = 'active'
  AND d.external_sheet_id = ''
  AND (
       (d.provider = 'wecom' AND d.source_type IN ('smartsheet_doc', 'registry_doc', 'smartsheet_link'))
    OR (d.provider = 'feishu' AND d.source_type = 'bitable_app')
  )
GROUP BY d.id, d.provider, d.env_profile, d.external_doc_id, d.source_type,
         d.document_name, d.source_name
ORDER BY d.provider, d.env_profile, d.id
"""


def assets(conn: Any, *, tplus_items: list[dict[str, Any]]) -> dict[str, Any]:
    return document_locator.asset_catalog(conn, tplus_items=tplus_items)


def _config_response(row: dict[str, Any], *, document: bool) -> dict[str, Any]:
    seconds = int(row.get("interval_seconds") or 86400)
    updated_by = str(row.get("updated_by") or "")
    result = {
        "enabled": bool(row.get("enabled", True)),
        "interval_seconds": seconds,
        "interval_hours": round(seconds / 3600, 4),
        "anchor_time": str(row.get("anchor_time") or ""),
        "updated_at": row.get("updated_at"),
        "updated_by": updated_by,
    }
    if document:
        result["pull_paused"] = bool(row.get("pull_paused", False))
        result["source"] = "飞书配置表" if updated_by == "feishu-config-table" else ("手动" if updated_by else "默认")
    return result


def _read_config(connect: Callable[[], Any], provider: str, *, document: bool) -> dict[str, Any]:
    defaults = _DOC_DEFAULTS if document else _TPLUS_DEFAULTS
    try:
        with closing(connect()) as conn:
            with conn.cursor() as cur:
                columns = "enabled, interval_seconds, anchor_time, pull_paused, updated_at, updated_by" if document else "enabled, interval_seconds, anchor_time, updated_at, updated_by"
                cur.execute(f"SELECT {columns} FROM integration_sync_config WHERE provider = %s", (provider,))
                row = cur.fetchone()
        if row:
            value = {
                "enabled": bool(row[0]),
                "interval_seconds": int(row[1]),
                "anchor_time": str(row[2] or ""),
            }
            offset = 3
            if document:
                value["pull_paused"] = bool(row[3])
                offset = 4
            value["updated_at"] = str(row[offset]) if row[offset] else None
            value["updated_by"] = str(row[offset + 1] or "")
            return _config_response(value, document=document)
    except Exception:
        pass
    return _config_response({**defaults, "updated_at": None, "updated_by": ""}, document=document)


def read_tplus_config(connect: Callable[[], Any]) -> dict[str, Any]:
    return _read_config(connect, "chanjet", document=False)


def read_doc_config(connect: Callable[[], Any]) -> dict[str, Any]:
    return _read_config(connect, "doc_sync", document=True)


def _save_config(connect: Callable[[], Any], body: SyncConfigUpdate, user_sub: str, *, document: bool) -> dict[str, Any]:
    interval_seconds = int(round(body.interval_hours * 3600))
    schedule = {"enabled": body.enabled, "interval_seconds": interval_seconds, "anchor_time": body.anchor_time}
    with closing(connect()) as conn:
        try:
            with conn.cursor() as cur:
                if document:
                    cur.execute(
                        """
                        INSERT INTO integration_sync_config(provider, enabled, interval_seconds, anchor_time, pull_paused, updated_at, updated_by)
                        VALUES ('doc_sync', %s, %s, %s, %s, NOW(), %s)
                        ON CONFLICT (provider) DO UPDATE SET
                            enabled=EXCLUDED.enabled,
                            interval_seconds=EXCLUDED.interval_seconds,
                            anchor_time=EXCLUDED.anchor_time,
                            pull_paused=EXCLUDED.pull_paused,
                            updated_at=NOW(),
                            updated_by=EXCLUDED.updated_by
                        """,
                        (body.enabled, interval_seconds, body.anchor_time, bool(getattr(body, "pull_paused", False)), user_sub),
                    )
                    cur.execute(
                        """
                        UPDATE sync_jobs SET schedule = %s, updated_at = NOW()
                        WHERE kind = 'pull' AND provider IN ('wecom', 'feishu')
                        RETURNING id
                        """,
                        (Jsonb(schedule),),
                    )
                    if not cur.fetchall():
                        raise RuntimeError("未找到文档同步 pull 作业")
                else:
                    cur.execute(
                        """
                        INSERT INTO integration_sync_config(provider, enabled, interval_seconds, anchor_time, updated_at, updated_by)
                        VALUES ('chanjet', %s, %s, %s, NOW(), %s)
                        ON CONFLICT (provider) DO UPDATE SET
                            enabled=EXCLUDED.enabled,
                            interval_seconds=EXCLUDED.interval_seconds,
                            anchor_time=EXCLUDED.anchor_time,
                            updated_at=NOW(),
                            updated_by=EXCLUDED.updated_by
                        """,
                        (body.enabled, interval_seconds, body.anchor_time, user_sub),
                    )
                    cur.execute(
                        """
                        UPDATE sync_jobs SET schedule = %s, updated_at = NOW()
                        WHERE job_key = 'chanjet.full' RETURNING job_key
                        """,
                        (Jsonb(schedule),),
                    )
                    if [str(row[0]) for row in cur.fetchall()] != ["chanjet.full"]:
                        raise RuntimeError("统一调度作业 chanjet.full 不存在或重复")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return read_doc_config(connect) if document else read_tplus_config(connect)


def save_tplus_config(connect: Callable[[], Any], body: SyncConfigUpdate, user_sub: str) -> dict[str, Any]:
    return _save_config(connect, body, user_sub, document=False)


def save_doc_config(connect: Callable[[], Any], body: DocSyncConfigUpdate, user_sub: str) -> dict[str, Any]:
    return _save_config(connect, body, user_sub, document=True)


def _existing_doc_request(cur: Any, source_id: int) -> tuple[int, str] | None:
    cur.execute(
        """
        SELECT id, status FROM sync_requests
        WHERE source_id=%s AND status IN ('pending', 'running')
        ORDER BY id DESC LIMIT 1
        """,
        (source_id,),
    )
    row = cur.fetchone()
    return (int(row[0]), str(row[1])) if row else None


def _insert_doc_request(cur: Any, source_id: int, provider: str, env_profile: str, requested_by: str) -> int:
    cur.execute(
        """
        INSERT INTO sync_requests(source_id, provider, env_profile, mode, status, requested_by)
        VALUES (%s, %s, %s, 'manual', 'pending', %s)
        RETURNING id
        """,
        (source_id, provider, env_profile, requested_by),
    )
    return int(cur.fetchone()[0])


def enqueue_doc_asset(conn: Any, source_id: int, requested_by: str) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT provider, env_profile, external_doc_id, source_type,
                       COALESCE(NULLIF(document_name, ''), NULLIF(source_name, ''), '未命名文档'), status
                FROM external_sources WHERE id=%s AND external_sheet_id=''
                """,
                (source_id,),
            )
            row = cur.fetchone()
            if not row:
                raise InvalidSyncTarget("同步文档不存在")
            provider, env_profile, external_doc_id, source_type, document_name, status = row
            syncable, reason = _doc_syncability(str(provider), str(source_type), str(external_doc_id or ""), str(status))
            if not syncable:
                raise InvalidSyncTarget(reason)
            existing = _existing_doc_request(cur, source_id)
            if existing:
                result = {"queued": False, "request_id": existing[0], "status": existing[1], "document_name": str(document_name)}
            else:
                request_id = _insert_doc_request(cur, source_id, str(provider), str(env_profile), requested_by)
                result = {"queued": True, "request_id": request_id, "status": "pending", "document_name": str(document_name)}
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


# 路由 POST /v1/sync/jobs/{job_key}/run 对这个 key 单独分派到 enqueue_tplus_full，
# 它没有表级 source_id，走不通 enqueue_doc_job 的约束。
SPECIAL_MANUAL_JOB_KEYS = {"chanjet.full"}


def manual_triggerable(job_key: str, kind: str, source_id: Any) -> bool:
    """「立即同步」按钮能不能点，与 sync_job_run 的实际分派共用同一份判据。

    前端此前用 job.enabled 自己推导，于是 kind='mirror' 的
    `wecom.locator_mirror｜企微文档定位档案镜像` 和 kind='reconcile' 的
    `tplus.parent_match｜T+ 父件核对` 也长出了按钮，点下去必然撞
    enqueue_doc_job 的 kind='pull' + 有效来源约束，报「同步作业不存在或不可手动触发」。
    """
    if str(job_key or "") in SPECIAL_MANUAL_JOB_KEYS:
        return True
    return str(kind or "") == "pull" and source_id is not None


def enqueue_doc_job(conn: Any, job_key: str, requested_by: str) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.source_id, s.provider, s.env_profile, s.source_type, s.status, j.enabled, j.display_name
                FROM sync_jobs j
                JOIN external_sources s ON s.id=j.source_id
                WHERE j.job_key=%s AND j.kind='pull'
                """,
                (job_key,),
            )
            row = cur.fetchone()
            if not row:
                raise InvalidSyncTarget("同步作业不存在或不可手动触发")
            source_id, provider, env_profile, source_type, status, enabled, display_name = row
            expected_type = WECOM_TABLE_TYPE if provider == "wecom" else FEISHU_TABLE_TYPE if provider == "feishu" else ""
            if not enabled or status != "active" or source_type != expected_type:
                raise InvalidSyncTarget("同步作业已停用或来源无效")
            existing = _existing_doc_request(cur, int(source_id))
            if existing:
                result = {"queued": False, "request_id": existing[0], "status": existing[1], "display_name": str(display_name)}
            else:
                request_id = _insert_doc_request(cur, int(source_id), str(provider), str(env_profile), requested_by)
                result = {"queued": True, "request_id": request_id, "status": "pending", "display_name": str(display_name)}
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _enqueue_tplus_on_cursor(cur: Any, user_sub: str) -> dict[str, Any]:
    cur.execute(PENDING_FULL_SYNC_SQL)
    existing = cur.fetchone()
    if existing:
        return {"queued": False, "request_id": int(existing[0]), "status": str(existing[1])}
    dedupe_key = f"manual-full-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{user_sub}"
    cur.execute(ENQUEUE_FULL_SYNC_SQL, (dedupe_key,))
    return {"queued": True, "request_id": int(cur.fetchone()[0]), "status": "pending"}


def enqueue_tplus_full(conn: Any, user_sub: str) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            result = _enqueue_tplus_on_cursor(cur, user_sub)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def enqueue_all(conn: Any, requested_by: str) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, provider, env_profile,
                       COALESCE(NULLIF(document_name, ''), NULLIF(source_name, ''), '未命名文档')
                FROM external_sources
                WHERE status='active' AND external_sheet_id=''
                  AND (
                       (provider='wecom' AND source_type IN ('smartsheet_doc', 'registry_doc')
                        AND external_doc_id LIKE 'dc%' AND length(external_doc_id) >= 80)
                    OR (provider='feishu' AND source_type='bitable_app' AND external_doc_id <> '')
                  )
                ORDER BY id
                """
            )
            documents = cur.fetchall()
            queued: list[int] = []
            skipped: list[int] = []
            for source_id, provider, env_profile, _document_name in documents:
                existing = _existing_doc_request(cur, int(source_id))
                if existing:
                    skipped.append(existing[0])
                else:
                    queued.append(_insert_doc_request(cur, int(source_id), str(provider), str(env_profile), requested_by))
            tplus = _enqueue_tplus_on_cursor(cur, requested_by)
        conn.commit()
        return {
            "documents_queued": len(queued),
            "documents_skipped": len(skipped),
            "document_request_ids": queued,
            "tplus_queued": bool(tplus["queued"]),
            "tplus_request_id": int(tplus["request_id"]),
            "message": f"已排队 {len(queued)} 个文档和 T+ 全量；跳过 {len(skipped)} 个已有请求。",
        }
    except Exception:
        conn.rollback()
        raise
