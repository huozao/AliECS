from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from scripts.adventurelog.al_client import AdventureLogClient
from scripts.adventurelog.transform import external_ref_for_memory, has_coords, memory_to_adventure


DEFAULT_REPORT = Path("docs/ops/adventurelog-migration-report-2026-06-14.md")


class MemorySource(Protocol):
    def iter_memories_with_coords(self):
        ...

    def photos_for_memory(self, memory_id: int):
        ...


class AdventureClient(Protocol):
    def list_existing_refs(self) -> set[str]:
        ...

    def create_adventure(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    def attach_immich_asset(self, adventure_id: int | str, asset_id: str) -> Any:
        ...


@dataclass
class MigrationResult:
    dry_run: bool
    processed: int = 0
    planned: int = 0
    created: int = 0
    skipped_existing: int = 0
    skipped_no_coords: int = 0
    attached_assets: int = 0
    manual_photos: list[dict[str, Any]] = field(default_factory=list)


class PostgresMemorySource:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise RuntimeError("ADVENTURELOG_SOURCE_DATABASE_URL or DATABASE_URL is required")
        self.database_url = database_url

    def iter_memories_with_coords(self):
        with psycopg.connect(self.database_url, connect_timeout=5, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      m.id, m.title, m.content, m.memory_date, m.place_name,
                      m.latitude, m.longitude, m.visibility,
                      COALESCE(array_agg(mt.tag ORDER BY mt.tag) FILTER (WHERE mt.tag IS NOT NULL), '{}') AS tags
                    FROM memories m
                    LEFT JOIN memory_tags mt ON mt.memory_id = m.id
                    WHERE m.archived = false
                      AND m.latitude IS NOT NULL
                      AND m.longitude IS NOT NULL
                    GROUP BY m.id
                    ORDER BY m.memory_date NULLS LAST, m.id
                    """
                )
                yield from cur.fetchall()

    def photos_for_memory(self, memory_id: int):
        with psycopg.connect(self.database_url, connect_timeout=5, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      id, storage_driver, external_library_type, external_asset_id,
                      original_filename, original_storage_url
                    FROM photos
                    WHERE memory_id = %s
                    ORDER BY id
                    """,
                    (memory_id,),
                )
                rows = list(cur.fetchall())
                cur.execute(
                    """
                    SELECT
                      id, provider AS storage_driver, 'immich' AS external_library_type,
                      immich_asset_id AS external_asset_id, original_filename,
                      thumbnail_cache_key AS original_storage_url
                    FROM couple_memory_assets
                    WHERE memory_id = %s
                    ORDER BY sort_order, id
                    """,
                    (memory_id,),
                )
                rows.extend(cur.fetchall())
        return rows


def run_migration(
    *,
    source: MemorySource | None = None,
    client: AdventureClient | None = None,
    dry_run: bool = True,
    report_path: str | Path = DEFAULT_REPORT,
) -> MigrationResult:
    source = source or PostgresMemorySource(_database_url_from_env())
    client = client or _client_from_env()
    report_path = Path(report_path)
    existing_refs = client.list_existing_refs()
    result = MigrationResult(dry_run=dry_run)

    for memory in source.iter_memories_with_coords():
        result.processed += 1
        if not has_coords(memory):
            result.skipped_no_coords += 1
            continue

        ref = external_ref_for_memory(memory)
        if ref in existing_refs:
            result.skipped_existing += 1
            continue

        payload = memory_to_adventure(memory)
        photos = list(source.photos_for_memory(int(memory["id"])))
        if dry_run:
            result.planned += 1
            _record_manual_photos(result, memory, photos)
            continue

        created = client.create_adventure(payload)
        result.created += 1
        existing_refs.add(ref)
        adventure_id = created.get("id") or created.get("pk")
        for photo in photos:
            asset_id = _immich_asset_id(photo)
            if asset_id and adventure_id is not None:
                client.attach_immich_asset(adventure_id, asset_id)
                result.attached_assets += 1
            elif not asset_id:
                result.manual_photos.append(_manual_photo(memory, photo))

    _write_report(report_path, result)
    return result


def _client_from_env() -> AdventureLogClient:
    base_url = os.getenv("ADVENTURELOG_BASE_URL", "https://adventure-media.hydwang.xyz").strip()
    token = os.getenv("ADVENTURELOG_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ADVENTURELOG_API_TOKEN is required")
    return AdventureLogClient(base_url, token)


def _database_url_from_env() -> str:
    return os.getenv("ADVENTURELOG_SOURCE_DATABASE_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()


def _immich_asset_id(photo: dict[str, Any]) -> str | None:
    driver = str(photo.get("storage_driver") or "").lower()
    library = str(photo.get("external_library_type") or "").lower()
    asset_id = photo.get("external_asset_id") or photo.get("immich_asset_id")
    if asset_id and (driver == "immich" or library == "immich"):
        return str(asset_id)
    return None


def _record_manual_photos(result: MigrationResult, memory: dict[str, Any], photos: list[dict[str, Any]]) -> None:
    for photo in photos:
        if not _immich_asset_id(photo):
            result.manual_photos.append(_manual_photo(memory, photo))


def _manual_photo(memory: dict[str, Any], photo: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": memory.get("id"),
        "memory_title": memory.get("title"),
        "storage_driver": photo.get("storage_driver"),
        "original_filename": photo.get("original_filename"),
        "original_storage_url": photo.get("original_storage_url"),
    }


def _write_report(path: Path, result: MigrationResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "dry-run" if result.dry_run else "apply"
    lines = [
        "# AdventureLog 迁移对账报告",
        "",
        f"- 模式：{mode}",
        f"- 处理 memories：{result.processed}",
        f"- 计划创建：{result.planned}",
        f"- 已创建：{result.created}",
        f"- 已存在跳过：{result.skipped_existing}",
        f"- 无坐标跳过：{result.skipped_no_coords}",
        f"- 已关联 Immich asset：{result.attached_assets}",
        "",
        "## 需人工处理照片",
        "",
    ]
    if result.manual_photos:
        for item in result.manual_photos:
            lines.append(
                "- memory #{memory_id} {memory_title} | {storage_driver} | {original_filename} | {original_storage_url}".format(
                    **{key: item.get(key) or "" for key in item}
                )
            )
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 实跑核对项",
            "",
            "- AdventureLog adventure 字段名、创建端点、Immich asset 关联端点需按部署版本文档或浏览器请求核对。",
            "- 若该版本 API 无 `external_ref` 字段，请将 `[ref:aliecs-memory:<id>]` 放入 description 并以此查重。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate AliECS couple memories to AdventureLog.")
    parser.add_argument("--apply", action="store_true", help="write to AdventureLog; default is dry-run")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="migration report path")
    parser.add_argument("--database-url", default="", help="override source Postgres URL")
    parser.add_argument("--base-url", default="", help="override AdventureLog backend URL")
    parser.add_argument("--token", default="", help="override AdventureLog API token")
    args = parser.parse_args()

    if args.database_url:
        os.environ["ADVENTURELOG_SOURCE_DATABASE_URL"] = args.database_url
    if args.base_url:
        os.environ["ADVENTURELOG_BASE_URL"] = args.base_url
    if args.token:
        os.environ["ADVENTURELOG_API_TOKEN"] = args.token

    result = run_migration(dry_run=not args.apply, report_path=args.report)
    print(
        f"mode={'dry-run' if result.dry_run else 'apply'} "
        f"processed={result.processed} planned={result.planned} created={result.created} "
        f"skipped_existing={result.skipped_existing} manual_photos={len(result.manual_photos)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
