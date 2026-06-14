from __future__ import annotations

from typing import Any


def _value(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def external_ref_for_memory(row: dict[str, Any]) -> str:
    return f"aliecs-memory:{row['id']}"


def has_coords(row: dict[str, Any]) -> bool:
    return _value(row, "lat", "latitude") is not None and _value(row, "lng", "longitude") is not None


def memory_to_adventure(row: dict[str, Any]) -> dict[str, Any]:
    ref = external_ref_for_memory(row)
    description = row.get("content") or ""
    # TODO(AdventureLog v0.12.1核对): 若 API 无 external_ref 字段，则将 ref 标记写入 description
    # 并让 AdventureLogClient.list_existing_refs() 从描述中解析 `[ref:aliecs-memory:<id>]`。
    return {
        # TODO(AdventureLog v0.12.1核对): name/location/latitude/visit_date/is_public 字段名以该版本 API 为准。
        "name": row["title"],
        "location": row.get("place_name"),
        "latitude": _value(row, "lat", "latitude"),
        "longitude": _value(row, "lng", "longitude"),
        "visit_date": str(row.get("memory_date")) if row.get("memory_date") else None,
        "description": description,
        "tags": list(row.get("tags") or []),
        "is_public": row.get("visibility") == "shareable",
        "external_ref": ref,
    }
