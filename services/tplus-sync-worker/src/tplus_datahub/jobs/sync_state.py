from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
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
    raw_records = _json_safe(rows)
    normalized = [_normalize_row(row) for row in raw_records]
    normalized.sort(key=lambda row: row["record_key"])
    items = _extract_bom_items(raw_records)
    items.sort(key=lambda item: item["record_key"])
    snapshot_hash = _stable_hash(normalized)
    return {
        "row_count": len(rows),
        "snapshot_hash": snapshot_hash,
        "records": normalized,
        "raw_records": raw_records,
        "items": items,
    }


def build_snapshot_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any] | None:
    if previous is None or previous.get("snapshot_hash") == current.get("snapshot_hash"):
        return None
    previous_count = int(previous.get("row_count") or 0)
    current_count = int(current.get("row_count") or 0)
    classification = classify_bom_changes(previous.get("items") or [], current.get("items") or [])
    needs_review = bool(classification["needs_review"])
    return {
        "status": "needs_review" if needs_review else "informational",
        "severity": "warning" if needs_review else "info",
        "summary": f"BOM full snapshot changed: rows {previous_count} -> {current_count}",
        "diff_json": {
            "previous_snapshot_id": previous.get("id"),
            "current_snapshot_id": current.get("id"),
            "previous_hash": previous.get("snapshot_hash"),
            "current_hash": current.get("snapshot_hash"),
            "previous_row_count": previous_count,
            "current_row_count": current_count,
            "row_count_delta": current_count - previous_count,
            "classification": {k: classification[k] for k in (
                "qty_changed", "material_changed", "bom_deleted", "bom_added",
                "status_changed", "cosmetic_changed", "needs_review")},
            "added": classification.get("added", []),
            "removed": classification.get("removed", []),
            "changed": classification.get("changed", []),
            "added_count": classification.get("added_count", 0),
            "removed_count": classification.get("removed_count", 0),
            "changed_count": classification.get("changed_count", 0),
        },
    }


@dataclass
class FullBomSnapshotResult:
    full_rows: list[Any]
    full_snapshot_id: int | None = None
    diff_summary: dict[str, Any] | None = None


def upsert_and_snapshot_full_bom(
    fetched_rows: list[Any], *, mode: str, source_json: dict[str, Any] | None = None
) -> FullBomSnapshotResult:
    """upsert 抓到的行 → 从 DB 拼全量 → 写全量快照 → 与上一份全量快照分类 diff →
    有变化即写 reconciliation 明细。返回用于导出的全量行(无 DB 时回退 fetched_rows)。"""
    if psycopg is None:
        return FullBomSnapshotResult(full_rows=fetched_rows)
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return FullBomSnapshotResult(full_rows=fetched_rows)
    try:
        with closing(psycopg.connect(database_url, connect_timeout=3)) as conn:
            normalized = [_normalize_row(r) for r in fetched_rows]
            with conn.cursor() as cur:
                _upsert_tplus_bom_records(cur, normalized)
                _mark_missing_records(cur, mode, normalized)
            conn.commit()
            full_rows = assemble_current_full_bom(conn)
            snapshot = snapshot_bom_rows(full_rows)
            source = dict(source_json or {})
            source.update({"mode": mode, "snapshot_hash": snapshot["snapshot_hash"],
                           "records": snapshot["raw_records"], "items": snapshot["items"]})
            previous = _latest_full_snapshot(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO integration_sync_snapshots(provider, module, mode, row_count, snapshot_hash, source_json)
                    VALUES ('chanjet', 'bom', %s, %s, %s, %s) RETURNING id
                    """,
                    (mode, snapshot["row_count"], snapshot["snapshot_hash"], Jsonb(source)),
                )
                snapshot["id"] = int(cur.fetchone()[0])
                diff = build_snapshot_diff(previous, snapshot)
                # 只要内容有变化就落一条明细（needs_review 与否只体现在 status/severity），
                # 否则页面上「本次变化」的详情只有被判需复核的那几次才看得到。
                if diff is not None:
                    cur.execute(
                        """
                        INSERT INTO integration_reconciliation_diffs(
                            provider, module, status, severity, summary, diff_json,
                            full_snapshot_id, incremental_snapshot_id)
                        VALUES ('chanjet', 'bom', %s, %s, %s, %s, %s, NULL)
                        """,
                        (diff["status"], diff["severity"], diff["summary"], Jsonb(diff["diff_json"]), snapshot["id"]),
                    )
            diff_summary = diff["diff_json"]["classification"] if diff is not None else None
            conn.commit()
            return FullBomSnapshotResult(
                full_rows=full_rows,
                full_snapshot_id=snapshot["id"],
                diff_summary=diff_summary,
            )
    except Exception:
        return FullBomSnapshotResult(full_rows=fetched_rows)


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
            SELECT id, row_count, snapshot_hash, source_json
            FROM integration_sync_snapshots
            WHERE provider = 'chanjet'
              AND module = 'bom'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if not row:
        return None
    source = _json_value(row[3])
    return {
        "id": row[0],
        "row_count": row[1],
        "snapshot_hash": row[2],
        "items": source.get("items") or _extract_bom_items(source.get("records") or []),
    }


def assemble_current_full_bom(conn: Any) -> list[Any]:
    """从 tplus_bom_records 取未失踪记录，按 (Code,Version) 去重(取 last_seen 最新)，返回原始行列表。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_json, last_seen_at
            FROM tplus_bom_records
            WHERE missing_since IS NULL
            ORDER BY last_seen_at ASC NULLS FIRST
            """
        )
        rows = cur.fetchall()
    by_key: dict[tuple[str, str], Any] = {}
    for raw, _seen in rows:
        record = raw if isinstance(raw, Mapping) else {}
        key = (str(record.get("Code") or record.get("code") or ""),
               str(record.get("Version") or record.get("version") or ""))
        by_key[key] = record  # ASC 排序 → 后写覆盖=最新 last_seen 胜出
    return list(by_key.values())


# 全量同步模式：本批应代表 T+ 当前全部 BOM，可据此剪枝失踪记录。增量(只抓单条)不可。
_FULL_SYNC_MODES = {"scheduled_full", "full_bom"}


def _mark_missing_records(cur: Any, mode: str, normalized: list[dict[str, Any]]) -> None:
    """全量同步把"本批未出现的活跃记录"标记 missing_since=NOW()——改名/删除的 BOM 不再
    泄漏到导出(原先 missing_since 从不被设置 → 僵尸记录永久残留)。
    增量模式批次是部分的，空批次(全量异常返回0行)也跳过，二者都不剪枝以防误清空。"""
    if mode not in _FULL_SYNC_MODES or not normalized:
        return
    cur.execute(
        "UPDATE tplus_bom_records SET missing_since = NOW() "
        "WHERE missing_since IS NULL AND record_key <> ALL(%s)",
        ([record["record_key"] for record in normalized],),
    )


def _upsert_tplus_bom_records(cur: Any, records: list[dict[str, Any]]) -> None:
    for record in records:
        cur.execute(
            """
            INSERT INTO tplus_bom_records(record_key, record_hash, raw_json, last_seen_at, missing_since)
            VALUES (%s, %s, %s, NOW(), NULL)
            ON CONFLICT(record_key) DO UPDATE
            SET record_hash = EXCLUDED.record_hash,
                raw_json = EXCLUDED.raw_json,
                last_seen_at = NOW(),
                missing_since = NULL
            """,
            (record["record_key"], record["record_hash"], Jsonb(record["raw"])),
        )


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


def _extract_bom_items(rows: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for parent_index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            value = _json_safe(row)
            items.append(
                {
                    "record_key": f"raw|{parent_index}",
                    "record_hash": _stable_hash(value),
                    "parent_code": "",
                    "parent_name": "",
                    "version": "",
                    "disabled": "",
                    "child_code": "",
                    "child_name": "",
                    "unit": "",
                    "quantity": None,
                    "raw": value,
                }
            )
            continue

        children = row.get("BOMChilds")
        if not isinstance(children, list) or not children:
            item = _bom_item_from_parent_child(row, None, parent_index, 0)
            items.append(item)
            continue

        for child_index, child in enumerate(children):
            item = _bom_item_from_parent_child(row, child if isinstance(child, Mapping) else None, parent_index, child_index)
            items.append(item)
    return items


def _bom_item_from_parent_child(
    parent: Mapping[str, Any],
    child: Mapping[str, Any] | None,
    parent_index: int,
    child_index: int,
) -> dict[str, Any]:
    child_row = child or {}
    key = {
        "parent_code": _text(parent.get("Code")),
        "version": _text(parent.get("Version")),
        "disabled": _text(parent.get("Disabled")),
        "child_code": _text(child_row.get("Code")),
        "child_id": _text(child_row.get("ID") or child_row.get("Id") or child_index),
    }
    comparable = {
        "parent_code": key["parent_code"],
        "parent_name": _text(parent.get("Name")),
        "version": key["version"],
        "disabled": key["disabled"],
        "default_bom": _text(parent.get("IsDefaultBom")),
        "child_code": key["child_code"],
        "child_name": _text(child_row.get("Name")),
        "unit": _text(_nested_value(child_row, "Unit", "Name")),
        "quantity": child_row.get("RequiredQuantity"),
        "memo": _text(child_row.get("Memo")),
        "waste_rate": child_row.get("WasteRate"),
    }
    if not key["parent_code"] and not key["version"] and not key["child_code"]:
        record_key = f"raw|{parent_index}|{child_index}"
    else:
        record_key = "|".join(str(key[name]) for name in ["parent_code", "version", "child_code", "child_id"])
    return {
        "record_key": record_key,
        "record_hash": _stable_hash(comparable),
        **comparable,
    }


_STATUS_FIELDS = {"disabled", "default_bom"}


def _parent_key(item: Mapping[str, Any]) -> tuple[str, str]:
    return (str(item.get("parent_code") or ""), str(item.get("version") or ""))


def classify_bom_changes(previous_items: list[Any], current_items: list[Any]) -> dict[str, Any]:
    """把 item 级 diff 归类。needs_review 仅当: 改数值 / 增删替换原料 / 删整条 BOM。"""
    item_diff = _diff_snapshot_items(previous_items, current_items)
    prev_parents = {_parent_key(i) for i in previous_items}
    cur_parents = {_parent_key(i) for i in current_items}
    deleted_parents = prev_parents - cur_parents
    added_parents = cur_parents - prev_parents

    material_changed = 0
    for item in (item_diff.get("added") or []) + (item_diff.get("removed") or []):
        pk = _parent_key(item)
        if pk not in added_parents and pk not in deleted_parents:
            material_changed += 1

    qty_changed = 0
    status_changed = 0
    cosmetic_changed = 0
    for change in item_diff.get("changed") or []:
        fields = set(change.get("changed_fields") or [])
        if "quantity" in fields or "child_code" in fields:
            qty_changed += 1
        elif fields and fields <= _STATUS_FIELDS:
            status_changed += 1
        elif fields:
            cosmetic_changed += 1

    needs_review = qty_changed > 0 or material_changed > 0 or len(deleted_parents) > 0
    return {
        "qty_changed": qty_changed,
        "material_changed": material_changed,
        "bom_deleted": len(deleted_parents),
        "bom_added": len(added_parents),
        "status_changed": status_changed,
        "cosmetic_changed": cosmetic_changed,
        "needs_review": needs_review,
        **item_diff,
    }


def _diff_snapshot_items(previous_items: list[Any], current_items: list[Any]) -> dict[str, Any]:
    previous = _items_by_key(previous_items)
    current = _items_by_key(current_items)
    added_keys = sorted(set(current) - set(previous))
    removed_keys = sorted(set(previous) - set(current))
    common_keys = sorted(set(previous) & set(current))
    changed: list[dict[str, Any]] = []
    for key in common_keys:
        before = previous[key]
        after = current[key]
        if before.get("record_hash") == after.get("record_hash"):
            continue
        changed_fields = [
            field
            for field in ["parent_name", "version", "disabled", "default_bom", "child_name", "unit", "quantity", "memo", "waste_rate"]
            if before.get(field) != after.get(field)
        ]
        changed.append(
            {
                "key": _item_key(after),
                "changed_fields": changed_fields,
                "before": _strip_item_meta(before),
                "after": _strip_item_meta(after),
            }
        )
    return {
        "added_count": len(added_keys),
        "removed_count": len(removed_keys),
        "changed_count": len(changed),
        "added": [_strip_item_meta(current[key]) for key in added_keys[:200]],
        "removed": [_strip_item_meta(previous[key]) for key in removed_keys[:200]],
        "changed": changed[:200],
    }


def _items_by_key(items: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        key = _text(item.get("record_key"))
        if key:
            result[key] = dict(item)
    return result


def _strip_item_meta(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in {"record_key", "record_hash", "raw"}}


def _item_key(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "parent_code": item.get("parent_code") or "",
        "version": item.get("version") or "",
        "disabled": item.get("disabled") or "",
        "child_code": item.get("child_code") or "",
    }


def _nested_value(row: Mapping[str, Any], key: str, nested_key: str) -> Any:
    value = row.get(key)
    if isinstance(value, Mapping):
        return value.get(nested_key)
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "obj"):
        obj = getattr(value, "obj")
        return obj if isinstance(obj, dict) else {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))

def _inventory_record_key(row: Any) -> str:
    """存货档案按 Code 唯一；取不到 Code 时退回内容哈希，保证 upsert 键稳定。"""
    if isinstance(row, dict):
        code = row.get("Code") or row.get("code")
        if code not in (None, ""):
            return str(code)
    return _stable_hash(row)


def upsert_inventory_records(cur: Any, records: list[dict[str, Any]]) -> None:
    for record in records:
        cur.execute(
            """
            INSERT INTO tplus_inventory_records(record_key, record_hash, raw_json, last_seen_at, missing_since)
            VALUES (%s, %s, %s, NOW(), NULL)
            ON CONFLICT(record_key) DO UPDATE
            SET record_hash = EXCLUDED.record_hash,
                raw_json = EXCLUDED.raw_json,
                last_seen_at = NOW(),
                missing_since = NULL
            """,
            (record["record_key"], record["record_hash"], Jsonb(record["raw"])),
        )


def mark_missing_inventory_records(cur: Any, mode: str, records: list[dict[str, Any]]) -> None:
    if mode not in _FULL_SYNC_MODES or not records:
        return
    cur.execute(
        "UPDATE tplus_inventory_records SET missing_since = NOW() "
        "WHERE missing_since IS NULL AND record_key <> ALL(%s)",
        ([record["record_key"] for record in records],),
    )


def persist_inventory_records(fetched_rows: list[Any], *, mode: str) -> None:
    """把 T+ 存货档案落到 tplus_inventory_records，供 doc-sync 核对与页面匹配；
    失败只告警不中断同步主流程（导出照常）。"""
    if psycopg is None:
        return
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return
    try:
        with closing(psycopg.connect(database_url, connect_timeout=3)) as conn:
            normalized = [
                {"record_key": _inventory_record_key(row), "record_hash": _stable_hash(row), "raw": row}
                for row in fetched_rows
            ]
            with conn.cursor() as cur:
                upsert_inventory_records(cur, normalized)
                mark_missing_inventory_records(cur, mode, normalized)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 落库失败不拖垮全量同步
        print(f"[tplus] 存货主数据落库失败（跳过，导出照常）：{exc}")
