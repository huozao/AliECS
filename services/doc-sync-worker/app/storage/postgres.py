from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # pragma: no cover - exercised when only pure helpers are unit-tested.
    psycopg = None  # type: ignore[assignment]

    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value: Any) -> None:
            self.value = value


@dataclass(frozen=True)
class RecordSnapshot:
    external_record_id: str
    record_hash: str
    raw_json: dict[str, Any]
    normalized_json: dict[str, Any]
    external_created_at: Any = None
    external_updated_at: Any = None


@dataclass(frozen=True)
class UpsertDecision:
    action: str
    should_write: bool


def database_url() -> str:
    return os.getenv("DATABASE_URL", "")


def connect() -> psycopg.Connection:
    if psycopg is None:
        raise RuntimeError("缺少 psycopg，请先安装 services/doc-sync-worker/requirements.txt。")
    url = database_url()
    if not url:
        raise RuntimeError("缺少 DATABASE_URL，无法写入 Postgres。")
    return psycopg.connect(url, connect_timeout=5)


_URL_KEYS = ("image_url", "url", "file_url", "download_url")
_CELL_TEXT_KEYS = ("text", "name", "value")
_CELL_LINK_KEYS = ("link", "url", "href")


def _cell_urls(items: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in _URL_KEYS:
            value = str(item.get(key) or "").strip()
            if value:
                urls.append(value)
                break
    return urls


def _cell_link_text(item: dict[str, Any]) -> str | None:
    text = ""
    for key in _CELL_TEXT_KEYS:
        if key in item:
            text = str(item.get(key) or "").strip()
            break
    link = ""
    for key in _CELL_LINK_KEYS:
        link = str(item.get(key) or "").strip()
        if link:
            break
    if not link:
        return text if text else None
    if not text or text == link:
        return link
    return f"{text} <{link}>"


def first_text_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        first = value[0]
        if isinstance(first, dict):
            link_text = _cell_link_text(first)
            if link_text is not None:
                return link_text
            # 图片/附件类元素没有文本键：提取全部 URL（如智能表格图片字段的 image_url）。
            urls = _cell_urls(value)
            if urls:
                return "; ".join(urls)
        return str(first).strip()
    if isinstance(value, dict):
        link_text = _cell_link_text(value)
        if link_text is not None:
            return link_text
    return str(value).strip()


def stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def external_record_id(record: dict[str, Any]) -> str:
    for key in ("record_id", "id", "recordId"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return stable_hash(record)


def record_values(record: dict[str, Any]) -> dict[str, Any]:
    for key in ("values", "fields"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return {}


def normalize_record(record: dict[str, Any], field_titles: dict[str, str]) -> dict[str, Any]:
    values = record_values(record)
    normalized: dict[str, Any] = {}
    for field_id, value in values.items():
        title = field_titles.get(str(field_id)) or str(field_id)
        normalized[title] = first_text_cell(value)
    return normalized


def compose_source_name(document_name: str, sheet_name: str) -> str:
    document = str(document_name or "").strip()
    sheet = str(sheet_name or "").strip()
    if document and sheet:
        return f"{document} / {sheet}"
    return document or sheet or "未命名表格"


def split_source_name(source_name: str) -> dict[str, str]:
    text = str(source_name or "").strip()
    if " / " in text:
        document_name, sheet_name = text.split(" / ", 1)
        return {"document_name": document_name.strip(), "sheet_name": sheet_name.strip()}
    return {"document_name": text, "sheet_name": ""}


def build_smartsheet_open_url(external_doc_id: str, external_sheet_id: str, source_url: str = "") -> str:
    if source_url:
        return source_url
    docid = str(external_doc_id or "").strip()
    sheet_id = str(external_sheet_id or "").strip()
    if not docid:
        return ""
    if sheet_id:
        return f"https://doc.weixin.qq.com/smartsheet/{docid}?sheet_id={sheet_id}"
    return f"https://doc.weixin.qq.com/smartsheet/{docid}"


def build_record_snapshot(record: dict[str, Any], field_titles: dict[str, str]) -> RecordSnapshot:
    raw_json = dict(record)
    normalized_json = normalize_record(raw_json, field_titles)
    return RecordSnapshot(
        external_record_id=external_record_id(raw_json),
        record_hash=stable_hash(raw_json),
        raw_json=raw_json,
        normalized_json=normalized_json,
        external_created_at=raw_json.get("create_time") or raw_json.get("created_at"),
        external_updated_at=raw_json.get("update_time") or raw_json.get("updated_at"),
    )


def decide_record_upsert(existing_hash: str | None, snapshot: RecordSnapshot) -> UpsertDecision:
    if existing_hash is None:
        return UpsertDecision(action="create", should_write=True)
    if existing_hash == snapshot.record_hash:
        return UpsertDecision(action="unchanged", should_write=False)
    return UpsertDecision(action="update", should_write=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PostgresDocSyncStore:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @classmethod
    def open(cls) -> "PostgresDocSyncStore":
        return cls(connect())

    def close(self) -> None:
        self.conn.close()

    def start_run(self, provider: str, env_profile: str, mode: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_runs(provider, env_profile, mode, status, started_at)
                VALUES (%s, %s, %s, 'running', NOW())
                RETURNING id
                """,
                (provider, env_profile, mode),
            )
            row = cur.fetchone()
        self.conn.commit()
        return int(row[0])

    def finish_run(self, run_id: int, status: str, counts: dict[str, int], error_json: list[dict[str, Any]]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_runs
                SET status = %s,
                    finished_at = NOW(),
                    source_count = %s,
                    sheet_count = %s,
                    record_count = %s,
                    created_count = %s,
                    updated_count = %s,
                    error_count = %s,
                    error_json = %s
                WHERE id = %s
                """,
                (
                    status,
                    counts.get("source_count", 0),
                    counts.get("sheet_count", 0),
                    counts.get("record_count", 0),
                    counts.get("created_count", 0),
                    counts.get("updated_count", 0),
                    counts.get("error_count", 0),
                    Jsonb(error_json),
                    run_id,
                ),
            )
        self.conn.commit()

    def ensure_source(
        self,
        provider: str,
        env_profile: str,
        source_name: str,
        source_type: str,
        external_doc_id: str,
        external_sheet_id: str,
        source_url: str = "",
        document_name: str = "",
        sheet_name: str = "",
    ) -> int:
        names = split_source_name(source_name)
        document_name = str(document_name or names["document_name"]).strip()
        sheet_name = str(sheet_name or names["sheet_name"]).strip()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO external_sources(
                    provider, env_profile, source_name, source_type,
                    external_doc_id, external_sheet_id, source_url,
                    document_name, sheet_name, status, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', NOW())
                ON CONFLICT(provider, env_profile, external_doc_id, external_sheet_id)
                DO UPDATE SET
                    source_name = EXCLUDED.source_name,
                    source_type = EXCLUDED.source_type,
                    source_url = EXCLUDED.source_url,
                    document_name = EXCLUDED.document_name,
                    sheet_name = EXCLUDED.sheet_name,
                    status = 'active',
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    provider,
                    env_profile,
                    source_name,
                    source_type,
                    external_doc_id,
                    external_sheet_id,
                    source_url,
                    document_name,
                    sheet_name,
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        return int(row[0])

    def upsert_doc_source(
        self,
        provider: str,
        env_profile: str,
        external_doc_id: str,
        document_name: str,
        source_url: str = "",
        external_modified_at: str = "",
    ) -> int:
        """doc 级登记行：保持实时文档名并记录 modify_time（增量跳过依据）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO external_sources(
                    provider, env_profile, source_name, source_type,
                    external_doc_id, external_sheet_id, source_url,
                    document_name, sheet_name, status, external_modified_at, updated_at
                )
                VALUES (%s, %s, %s, 'smartsheet_doc', %s, '', %s, %s, '', 'active', %s, NOW())
                ON CONFLICT(provider, env_profile, external_doc_id, external_sheet_id)
                DO UPDATE SET
                    source_name = EXCLUDED.source_name,
                    document_name = EXCLUDED.document_name,
                    external_modified_at = EXCLUDED.external_modified_at,
                    status = 'active',
                    updated_at = NOW()
                RETURNING id
                """,
                (provider, env_profile, document_name, external_doc_id, source_url, document_name, external_modified_at),
            )
            row = cur.fetchone()
        self.conn.commit()
        return int(row[0])

    def upsert_structure_document(
        self,
        *,
        provider: str,
        env_profile: str,
        source_type: str,
        external_doc_id: str,
        document_name: str,
        source_url: str = "",
    ) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO external_sources(
                    provider, env_profile, source_name, source_type,
                    external_doc_id, external_sheet_id, source_url,
                    document_name, sheet_name, status, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, '', %s, %s, '', 'active', NOW())
                ON CONFLICT(provider, env_profile, external_doc_id, external_sheet_id)
                DO UPDATE SET
                    source_name = EXCLUDED.source_name,
                    source_type = EXCLUDED.source_type,
                    source_url = EXCLUDED.source_url,
                    document_name = EXCLUDED.document_name,
                    status = 'active',
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    provider,
                    env_profile,
                    document_name,
                    source_type,
                    external_doc_id,
                    source_url,
                    document_name,
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        return int(row[0])

    def deactivate_missing_structure_sheets(
        self,
        *,
        provider: str,
        env_profile: str,
        external_doc_id: str,
        active_sheet_ids: list[str],
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE external_sources
                SET status = 'inactive', updated_at = NOW()
                WHERE provider = %s
                  AND env_profile = %s
                  AND external_doc_id = %s
                  AND source_type = 'structure_backup_sheet'
                  AND external_sheet_id <> ''
                  AND external_sheet_id <> ALL(%s)
                """,
                (provider, env_profile, external_doc_id, active_sheet_ids),
            )
        self.conn.commit()

    def get_doc_modified(self, provider: str, env_profile: str, external_doc_id: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT external_modified_at FROM external_sources
                WHERE provider = %s AND env_profile = %s AND external_doc_id = %s AND external_sheet_id = ''
                """,
                (provider, env_profile, external_doc_id),
            )
            row = cur.fetchone()
        return str(row[0] or "") if row else ""

    def list_registry_doc_sources(self, provider: str, env_profile: str) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_name, source_type, external_doc_id, source_url
                FROM external_sources
                WHERE provider = %s
                  AND env_profile = %s
                  AND status = 'active'
                  AND source_type IN ('smartsheet_doc', 'registry_doc')
                  AND external_doc_id <> ''
                ORDER BY id
                """,
                (provider, env_profile),
            )
            rows = cur.fetchall()
        return [
            {
                "source_name": row[0],
                "source_type": row[1],
                "external_doc_id": row[2],
                "source_url": row[3] or "",
            }
            for row in rows
        ]

    def list_bitable_sources(self, provider: str, env_profile: str) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    source_name, source_type, external_doc_id, external_sheet_id,
                    source_url, document_name, sheet_name
                FROM external_sources
                WHERE provider = %s
                  AND env_profile = %s
                  AND status = 'active'
                  AND source_type = 'bitable_table'
                  AND external_doc_id <> ''
                  AND external_sheet_id <> ''
                ORDER BY id
                """,
                (provider, env_profile),
            )
            rows = cur.fetchall()
        return [
            {
                "source_name": row[0],
                "source_type": row[1],
                "external_doc_id": row[2],
                "external_sheet_id": row[3],
                "source_url": row[4] or "",
                "document_name": row[5] or "",
                "sheet_name": row[6] or "",
            }
            for row in rows
        ]

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, provider, env_profile, source_name, source_type,
                    external_doc_id, external_sheet_id, source_url, status, sheet_name
                FROM external_sources
                WHERE id = %s
                """,
                (source_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "provider": row[1],
            "env_profile": row[2],
            "source_name": row[3],
            "source_type": row[4],
            "external_doc_id": row[5],
            "external_sheet_id": row[6],
            "source_url": row[7] or "",
            "status": row[8],
            "sheet_name": row[9] or "",
        }

    def pending_sync_requests(self, limit: int) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_id, provider, env_profile, mode, requested_by, requested_at
                FROM sync_requests
                WHERE status = 'pending'
                ORDER BY requested_at ASC, id ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "source_id": row[1],
                "provider": row[2],
                "env_profile": row[3],
                "mode": row[4],
                "requested_by": row[5],
                "requested_at": str(row[6]),
            }
            for row in rows
        ]

    def mark_sync_request_running(self, request_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE sync_requests SET status = 'running', started_at = NOW() WHERE id = %s AND status = 'pending'",
                (request_id,),
            )
        self.conn.commit()

    def finish_sync_request(
        self,
        request_id: int,
        status: str,
        sync_run_id: int | None,
        error_json: dict[str, Any] | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_requests
                SET status = %s,
                    finished_at = NOW(),
                    sync_run_id = %s,
                    error_json = %s
                WHERE id = %s
                """,
                (status, sync_run_id, Jsonb(error_json or {}), request_id),
            )
        self.conn.commit()

    def enqueue_structure_backup_job(self, source_id: int, trigger: str, event_key: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wecom_structure_backup_jobs(source_id, event_key, trigger)
                VALUES (%s, %s, %s)
                ON CONFLICT(event_key) DO NOTHING
                """,
                (source_id, event_key, trigger),
            )
        self.conn.commit()

    def pending_structure_backup_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_id, event_key, trigger, attempt_count, created_at
                FROM wecom_structure_backup_jobs
                WHERE status = 'pending' AND next_attempt_at <= NOW()
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "source_id": row[1],
                "event_key": row[2],
                "trigger": row[3],
                "attempt_count": row[4],
                "created_at": str(row[5]),
            }
            for row in rows
        ]

    def claim_structure_backup_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                WITH ready AS (
                    SELECT id
                    FROM wecom_structure_backup_jobs
                    WHERE (status = 'pending' AND next_attempt_at <= NOW())
                       OR (status = 'running' AND started_at < NOW() - INTERVAL '15 minutes')
                    ORDER BY next_attempt_at ASC, id ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE wecom_structure_backup_jobs AS jobs
                SET status = 'running', started_at = NOW()
                FROM ready
                WHERE jobs.id = ready.id
                RETURNING jobs.id, jobs.source_id, jobs.event_key, jobs.trigger,
                          jobs.attempt_count, jobs.created_at
                """,
                (limit,),
            )
            rows = cur.fetchall()
        self.conn.commit()
        return [
            {
                "id": row[0],
                "source_id": row[1],
                "event_key": row[2],
                "trigger": row[3],
                "attempt_count": row[4],
                "created_at": str(row[5]),
            }
            for row in rows
        ]

    def mark_structure_backup_job_running(self, job_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wecom_structure_backup_jobs
                SET status = 'running', started_at = NOW()
                WHERE id = %s AND status = 'pending'
                """,
                (job_id,),
            )
        self.conn.commit()

    def retry_structure_backup_job(self, job_id: int, error: str, delay_seconds: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wecom_structure_backup_jobs
                SET status = 'pending',
                    attempt_count = attempt_count + 1,
                    last_error = %s,
                    next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                    started_at = NULL
                WHERE id = %s
                """,
                (str(error)[:2000], max(1, int(delay_seconds)), job_id),
            )
        self.conn.commit()

    def finish_structure_backup_job(self, job_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wecom_structure_backup_jobs
                SET status = 'success', finished_at = NOW(), last_error = ''
                WHERE id = %s
                """,
                (job_id,),
            )
        self.conn.commit()

    def list_wecom_document_structures(self, source_id: int | None = None) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    d.id, d.env_profile, d.external_doc_id, d.document_name,
                    d.source_url, d.source_type, d.status, d.external_modified_at,
                    d.last_sync_at,
                    (
                        SELECT MAX(r.requested_at)
                        FROM sync_requests r
                        WHERE r.source_id = d.id AND r.requested_by = 'copy-auto'
                    ) AS copy_requested_at,
                    s.id, s.external_sheet_id, s.sheet_name, s.source_type, s.last_sync_at,
                    f.id, f.external_field_id, f.field_title, f.field_type, f.raw_json
                FROM external_sources d
                LEFT JOIN external_sources s
                    ON s.provider = d.provider
                   AND s.env_profile = d.env_profile
                   AND s.external_doc_id = d.external_doc_id
                   AND s.external_sheet_id <> ''
                   AND s.status = 'active'
                LEFT JOIN external_fields f ON f.source_id = s.id
                WHERE d.provider = 'wecom'
                  AND d.external_sheet_id = ''
                  AND d.status = 'active'
                  AND (CAST(%s AS BIGINT) IS NULL OR d.id = %s)
                ORDER BY d.id, s.id, f.id
                """,
                (source_id, source_id),
            )
            rows = cur.fetchall()

        documents: dict[int, dict[str, Any]] = {}
        sheets: dict[tuple[int, int], dict[str, Any]] = {}
        for row in rows:
            document_id = int(row[0])
            document = documents.setdefault(
                document_id,
                {
                    "id": document_id,
                    "provider": "wecom",
                    "env_profile": row[1] or "",
                    "external_doc_id": row[2] or "",
                    "document_name": row[3] or "",
                    "source_url": row[4] or "",
                    "source_type": row[5] or "",
                    "status": row[6] or "",
                    "external_modified_at": row[7],
                    "last_sync_at": row[8],
                    "copy_requested_at": row[9],
                    "sheets": [],
                },
            )
            if row[10] is None:
                continue
            sheet_source_id = int(row[10])
            sheet_key = (document_id, sheet_source_id)
            sheet = sheets.get(sheet_key)
            if sheet is None:
                sheet = {
                    "source_id": sheet_source_id,
                    "external_sheet_id": row[11] or "",
                    "sheet_name": row[12] or "",
                    "source_type": row[13] or "",
                    "last_sync_at": row[14],
                    "fields": [],
                }
                sheets[sheet_key] = sheet
                document["sheets"].append(sheet)
            if row[15] is not None:
                sheet["fields"].append(
                    {
                        "order": len(sheet["fields"]) + 1,
                        "external_field_id": row[16] or "",
                        "field_title": row[17] or "",
                        "field_type": row[18] or "",
                        "raw_json": row[19] if isinstance(row[19], dict) else {},
                    }
                )
        return list(documents.values())

    def list_feishu_document_structures(self, source_id: int | None = None) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    d.id, d.env_profile, d.external_doc_id, d.document_name,
                    d.source_url, d.source_type, d.status, d.last_sync_at,
                    s.id, s.external_sheet_id, s.sheet_name, s.source_type, s.last_sync_at,
                    f.id, f.external_field_id, f.field_title, f.field_type, f.raw_json
                FROM external_sources d
                LEFT JOIN external_sources s
                    ON s.provider = d.provider
                   AND s.env_profile = d.env_profile
                   AND s.external_doc_id = d.external_doc_id
                   AND s.source_type = 'bitable_table'
                   AND s.external_sheet_id <> ''
                   AND s.status = 'active'
                LEFT JOIN external_fields f ON f.source_id = s.id
                WHERE d.provider = 'feishu'
                  AND d.source_type = 'bitable_app'
                  AND d.external_sheet_id = ''
                  AND d.status = 'active'
                  AND (CAST(%s AS BIGINT) IS NULL OR d.id = %s)
                ORDER BY d.id, s.id, f.id
                """,
                (source_id, source_id),
            )
            rows = cur.fetchall()

        documents: dict[int, dict[str, Any]] = {}
        sheets: dict[tuple[int, int], dict[str, Any]] = {}
        for row in rows:
            document_id = int(row[0])
            document = documents.setdefault(
                document_id,
                {
                    "id": document_id,
                    "provider": "feishu",
                    "env_profile": row[1] or "",
                    "external_doc_id": row[2] or "",
                    "document_name": row[3] or "",
                    "source_url": row[4] or "",
                    "source_type": row[5] or "",
                    "status": row[6] or "",
                    "external_modified_at": "",
                    "last_sync_at": row[7],
                    "copy_requested_at": None,
                    "sheets": [],
                },
            )
            if row[8] is None:
                continue
            table_source_id = int(row[8])
            sheet_key = (document_id, table_source_id)
            sheet = sheets.get(sheet_key)
            if sheet is None:
                sheet = {
                    "source_id": table_source_id,
                    "external_sheet_id": row[9] or "",
                    "sheet_name": row[10] or "",
                    "source_type": row[11] or "",
                    "last_sync_at": row[12],
                    "fields": [],
                }
                sheets[sheet_key] = sheet
                document["sheets"].append(sheet)
            if row[13] is not None:
                sheet["fields"].append(
                    {
                        "order": len(sheet["fields"]) + 1,
                        "external_field_id": row[14] or "",
                        "field_title": row[15] or "",
                        "field_type": row[16] or "",
                        "raw_json": row[17] if isinstance(row[17], dict) else {},
                    }
                )
        return list(documents.values())

    def replace_fields(self, source_id: int, fields: list[dict[str, Any]]) -> dict[str, str]:
        field_titles: dict[str, str] = {}
        current_field_ids: list[str] = []
        with self.conn.cursor() as cur:
            for field in fields:
                field_id = str(field.get("field_id") or field.get("id") or field.get("key") or "")
                if not field_id:
                    continue
                current_field_ids.append(field_id)
                title = str(
                    field.get("field_title")
                    or field.get("field_name")
                    or field.get("title")
                    or field.get("name")
                    or field_id
                )
                field_titles[field_id] = title
                cur.execute(
                    """
                    INSERT INTO external_fields(source_id, external_field_id, field_title, field_type, raw_json, synced_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT(source_id, external_field_id)
                    DO UPDATE SET
                        field_title = EXCLUDED.field_title,
                        field_type = EXCLUDED.field_type,
                        raw_json = EXCLUDED.raw_json,
                        synced_at = NOW()
                    """,
                    (
                        source_id,
                        field_id,
                        title,
                        str(field.get("field_type") or field.get("type") or ""),
                        Jsonb(field),
                    ),
                )
            cur.execute(
                """
                DELETE FROM external_fields
                WHERE source_id = %s AND external_field_id <> ALL(%s)
                """,
                (source_id, current_field_ids),
            )
        self.conn.commit()
        return field_titles

    def upsert_record(self, source_id: int, snapshot: RecordSnapshot) -> UpsertDecision:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT record_hash, normalized_json FROM external_records WHERE source_id = %s AND external_record_id = %s",
                (source_id, snapshot.external_record_id),
            )
            row = cur.fetchone()
            existing_hash = str(row[0]) if row else None
            decision = decide_record_upsert(existing_hash, snapshot)
            if row and decision.action == "unchanged" and row[1] != snapshot.normalized_json:
                decision = UpsertDecision(action="update", should_write=True)
            if decision.action == "create":
                cur.execute(
                    """
                    INSERT INTO external_records(
                        source_id, external_record_id, record_hash, raw_json, normalized_json,
                        external_created_at, external_updated_at, synced_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (
                        source_id,
                        snapshot.external_record_id,
                        snapshot.record_hash,
                        Jsonb(snapshot.raw_json),
                        Jsonb(snapshot.normalized_json),
                        snapshot.external_created_at,
                        snapshot.external_updated_at,
                    ),
                )
            elif decision.action == "update":
                cur.execute(
                    """
                    UPDATE external_records
                    SET record_hash = %s,
                        raw_json = %s,
                        normalized_json = %s,
                        external_created_at = %s,
                        external_updated_at = %s,
                        synced_at = NOW()
                    WHERE source_id = %s AND external_record_id = %s
                    """,
                    (
                        snapshot.record_hash,
                        Jsonb(snapshot.raw_json),
                        Jsonb(snapshot.normalized_json),
                        snapshot.external_created_at,
                        snapshot.external_updated_at,
                        source_id,
                        snapshot.external_record_id,
                    ),
                )
        self.conn.commit()
        return decision

    def delete_missing_records(self, source_id: int, external_record_ids: list[str]) -> int:
        with self.conn.cursor() as cur:
            if external_record_ids:
                cur.execute(
                    """
                    DELETE FROM external_records
                    WHERE source_id = %s
                      AND NOT (external_record_id = ANY(%s))
                    """,
                    (source_id, external_record_ids),
                )
            else:
                cur.execute("DELETE FROM external_records WHERE source_id = %s", (source_id,))
            deleted_count = int(cur.rowcount or 0)
        self.conn.commit()
        return deleted_count

    def disable_missing_sheets(
        self, provider: str, env_profile: str, external_doc_id: str, seen_sheet_ids: list[str]
    ) -> int:
        if not seen_sheet_ids:
            return 0
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE external_sources
                SET status = 'disabled',
                    updated_at = NOW()
                WHERE provider = %s
                  AND env_profile = %s
                  AND external_doc_id = %s
                  AND external_sheet_id <> ''
                  AND status = 'active'
                  AND NOT (external_sheet_id = ANY(%s))
                """,
                (provider, env_profile, external_doc_id, seen_sheet_ids),
            )
            disabled_count = int(cur.rowcount or 0)
        self.conn.commit()
        return disabled_count

    def list_image_backfill_targets(self, profiles: list[str] | None = None) -> list[dict[str, Any]]:
        profiles = [str(item).strip() for item in (profiles or []) if str(item).strip()]
        params: list[Any] = []
        profile_filter = ""
        if profiles:
            profile_filter = "AND env_profile = ANY(%s)"
            params.append(profiles)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, provider, env_profile, external_doc_id, sheet_title,
                       attachment_field_title, image_field_title
                FROM image_backfill_targets
                WHERE enabled = TRUE
                  AND provider = 'wecom'
                  {profile_filter}
                ORDER BY id
                """,
                params,
            )
            rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "provider": row[1],
                "env_profile": row[2],
                "external_doc_id": row[3],
                "sheet_title": row[4],
                "attachment_field_title": row[5],
                "image_field_title": row[6],
            }
            for row in rows
        ]

    def get_image_backfill_status(self, external_doc_id: str, sheet_id: str, record_id: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT status
                FROM image_backfill_log
                WHERE external_doc_id = %s AND sheet_id = %s AND record_id = %s
                """,
                (external_doc_id, sheet_id, record_id),
            )
            row = cur.fetchone()
        return str(row[0] or "") if row else ""

    def upsert_image_backfill_log(
        self,
        *,
        provider: str,
        env_profile: str,
        external_doc_id: str,
        sheet_id: str,
        record_id: str,
        sp_no: str,
        status: str,
        image_count: int = 0,
        error: str = "",
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO image_backfill_log(
                    provider, env_profile, external_doc_id, sheet_id, record_id,
                    sp_no, status, image_count, error, attempted_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT(external_doc_id, sheet_id, record_id)
                DO UPDATE SET
                    provider = EXCLUDED.provider,
                    env_profile = EXCLUDED.env_profile,
                    sp_no = EXCLUDED.sp_no,
                    status = EXCLUDED.status,
                    image_count = EXCLUDED.image_count,
                    error = EXCLUDED.error,
                    attempted_at = NOW(),
                    updated_at = NOW()
                """,
                (provider, env_profile, external_doc_id, sheet_id, record_id, sp_no, status, image_count, error),
            )
        self.conn.commit()

    def upsert_managed_contact(self, contact: dict[str, Any]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO managed_contacts(
                    channel, peer_id, display_name, remark, enabled, project_url,
                    project_name, tags, daily_quota, notes, source_sheet, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(channel, peer_id)
                DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    remark = EXCLUDED.remark,
                    enabled = EXCLUDED.enabled,
                    project_url = EXCLUDED.project_url,
                    project_name = EXCLUDED.project_name,
                    tags = EXCLUDED.tags,
                    daily_quota = EXCLUDED.daily_quota,
                    notes = EXCLUDED.notes,
                    source_sheet = EXCLUDED.source_sheet,
                    updated_at = NOW()
                """,
                (
                    contact.get("channel"),
                    contact.get("peer_id"),
                    contact.get("display_name"),
                    contact.get("remark"),
                    bool(contact.get("enabled", True)),
                    contact.get("project_url"),
                    contact.get("project_name"),
                    contact.get("tags"),
                    contact.get("daily_quota"),
                    contact.get("notes"),
                    contact.get("source_sheet"),
                ),
            )
        self.conn.commit()

    def mark_source_synced(self, source_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE external_sources SET last_sync_at = NOW(), updated_at = NOW() WHERE id = %s", (source_id,))
        self.conn.commit()

    # --- 文档同步调度配置（integration_sync_config，与 backend 共用） ---
    def get_sync_config(self, provider: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT enabled, interval_seconds, anchor_time, pull_paused, updated_at, updated_by "
                "FROM integration_sync_config WHERE provider = %s",
                (provider,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "enabled": bool(row[0]),
            "interval_seconds": int(row[1]),
            "anchor_time": str(row[2] or ""),
            "pull_paused": bool(row[3]),
            "updated_at": row[4],
            "updated_by": str(row[5] or ""),
        }

    def upsert_sync_config(
        self, provider: str, enabled: bool, interval_seconds: int, anchor_time: str, updated_by: str
    ) -> None:
        """写调度配置（不碰 pull_paused，那是管理页应急覆盖的专属开关）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO integration_sync_config(provider, enabled, interval_seconds, anchor_time, updated_at, updated_by)
                VALUES (%s, %s, %s, %s, NOW(), %s)
                ON CONFLICT (provider) DO UPDATE
                SET enabled = EXCLUDED.enabled,
                    interval_seconds = EXCLUDED.interval_seconds,
                    anchor_time = EXCLUDED.anchor_time,
                    updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by
                """,
                (provider, enabled, interval_seconds, anchor_time, updated_by),
            )
        self.conn.commit()

    def last_full_run_started_at(self) -> datetime | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(started_at) FROM sync_runs WHERE mode = 'full' AND provider IN ('wecom', 'feishu')"
            )
            row = cur.fetchone()
        return row[0] if row and row[0] else None

    # --- 群研发过程记录：群↔需求绑定 + 群消息入库 ---
    def upsert_group_binding(
        self,
        *,
        provider: str,
        env_profile: str,
        chatid: str,
        external_doc_id: str,
        sheet_title: str,
        record_id: str,
        requirement_key: str,
        bound_by: str = "",
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO group_record_map(
                    provider, env_profile, chatid, external_doc_id, sheet_title,
                    record_id, requirement_key, bound_by, bound_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT(chatid) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    env_profile = EXCLUDED.env_profile,
                    external_doc_id = EXCLUDED.external_doc_id,
                    sheet_title = EXCLUDED.sheet_title,
                    record_id = EXCLUDED.record_id,
                    requirement_key = EXCLUDED.requirement_key,
                    bound_by = EXCLUDED.bound_by,
                    updated_at = NOW()
                """,
                (provider, env_profile, chatid, external_doc_id, sheet_title, record_id, requirement_key, bound_by),
            )
        self.conn.commit()

    def get_group_binding(self, chatid: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT chatid, external_doc_id, sheet_title, record_id, requirement_key
                FROM group_record_map WHERE chatid = %s
                """,
                (chatid,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "chatid": row[0],
            "external_doc_id": row[1],
            "sheet_title": row[2],
            "record_id": row[3],
            "requirement_key": row[4],
        }

    def insert_group_message(
        self,
        *,
        msgid: str,
        chatid: str,
        from_userid: str,
        msgtype: str,
        text_content: str,
        quote_json: Any,
        media_paths: Any,
        record_id: str,
        ts: Any,
        raw_json: Any,
    ) -> bool:
        """入库一条群消息；按 msgid 幂等。返回 True=新插入，False=已存在（重推跳过）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO group_messages(
                    msgid, chatid, from_userid, msgtype, text_content,
                    quote_json, media_paths, record_id, ts, raw_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(msgid) DO NOTHING
                RETURNING id
                """,
                (
                    msgid,
                    chatid,
                    from_userid,
                    msgtype,
                    text_content,
                    Jsonb(quote_json or {}),
                    Jsonb(media_paths or []),
                    record_id,
                    ts,
                    Jsonb(raw_json or {}),
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        return row is not None

    def assign_chat_messages_to_record(self, chatid: str, record_id: str) -> int:
        """绑定后，把该群此前未归属的消息回填 record_id。返回回填条数。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE group_messages SET record_id = %s WHERE chatid = %s AND COALESCE(record_id, '') = ''",
                (record_id, chatid),
            )
            count = int(cur.rowcount or 0)
        self.conn.commit()
        return count

    def mark_message_node(self, msgid: str, category: str = "", summary: str = "") -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE group_messages
                SET is_node = TRUE, node_category = %s, node_summary = %s
                WHERE msgid = %s
                RETURNING id
                """,
                (category, summary, msgid),
            )
            row = cur.fetchone()
        self.conn.commit()
        return row is not None

    def list_pending_node_messages(self, limit: int = 50) -> list[dict[str, Any]]:
        """已标节点、有归属需求、尚未写入子表的消息。"""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT msgid, chatid, from_userid, msgtype, text_content, node_category,
                       node_summary, media_paths, record_id, quote_json, created_at
                FROM group_messages
                WHERE is_node = TRUE AND written_to_sheet = FALSE AND COALESCE(record_id, '') <> ''
                ORDER BY id
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "msgid": r[0],
                "chatid": r[1],
                "from_userid": r[2],
                "msgtype": r[3],
                "text_content": r[4],
                "node_category": r[5],
                "node_summary": r[6],
                "media_paths": r[7] or [],
                "record_id": r[8],
                "quote_json": r[9] or {},
                "created_at": r[10],
            }
            for r in rows
        ]

    def mark_message_written(self, msgid: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE group_messages SET written_to_sheet = TRUE WHERE msgid = %s", (msgid,))
        self.conn.commit()


def open_store() -> PostgresDocSyncStore:
    return PostgresDocSyncStore.open()


def close_store(store: PostgresDocSyncStore) -> None:
    with closing(store.conn):
        pass
