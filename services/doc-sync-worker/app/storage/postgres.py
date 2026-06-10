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


def first_text_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        first = value[0]
        if isinstance(first, dict):
            for key in ("text", "name", "value"):
                if key in first:
                    return str(first.get(key) or "").strip()
            # 图片/附件类元素没有文本键：提取全部 URL（如智能表格图片字段的 image_url）。
            urls = _cell_urls(value)
            if urls:
                return "; ".join(urls)
        return str(first).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            if key in value:
                return str(value.get(key) or "").strip()
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

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, provider, env_profile, source_name, source_type,
                    external_doc_id, external_sheet_id, source_url, status
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

    def replace_fields(self, source_id: int, fields: list[dict[str, Any]]) -> dict[str, str]:
        field_titles: dict[str, str] = {}
        with self.conn.cursor() as cur:
            for field in fields:
                field_id = str(field.get("field_id") or field.get("id") or field.get("key") or "")
                if not field_id:
                    continue
                title = str(field.get("field_title") or field.get("title") or field.get("name") or field_id)
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
        self.conn.commit()
        return field_titles

    def upsert_record(self, source_id: int, snapshot: RecordSnapshot) -> UpsertDecision:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT record_hash FROM external_records WHERE source_id = %s AND external_record_id = %s",
                (source_id, snapshot.external_record_id),
            )
            row = cur.fetchone()
            existing_hash = str(row[0]) if row else None
            decision = decide_record_upsert(existing_hash, snapshot)
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

    def mark_source_synced(self, source_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE external_sources SET last_sync_at = NOW(), updated_at = NOW() WHERE id = %s", (source_id,))
        self.conn.commit()


def open_store() -> PostgresDocSyncStore:
    return PostgresDocSyncStore.open()


def close_store(store: PostgresDocSyncStore) -> None:
    with closing(store.conn):
        pass
