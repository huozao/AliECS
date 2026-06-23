from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
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
    item_diff = _diff_snapshot_items(previous.get("items") or [], current.get("items") or [])
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
            **item_diff,
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
    source["snapshot_hash"] = snapshot["snapshot_hash"]
    source["records"] = snapshot["raw_records"]
    source["items"] = snapshot["items"]
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
                _upsert_tplus_bom_records(cur, snapshot["records"])
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
            SELECT id, row_count, snapshot_hash, source_json
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
    source = _json_value(row[3])
    return {
        "id": row[0],
        "row_count": row[1],
        "snapshot_hash": row[2],
        "items": source.get("items") or _extract_bom_items(source.get("records") or []),
    }


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
