from __future__ import annotations

from typing import Any, Mapping


def _flatten_mapping(row: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in row.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            flattened.update(_flatten_mapping(value, full_key))
        else:
            flattened[full_key] = value
    return flattened


def transform_partner_rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [_flatten_mapping(row) if isinstance(row, Mapping) else {"value": row} for row in rows]
