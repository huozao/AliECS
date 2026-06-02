from __future__ import annotations

from typing import Any

ROOT_KEYS = ("Result", "Data", "Value", "result", "data")
LIST_KEYS = ("Rows", "rows", "List", "list", "Items", "items", "Records", "records", "Data", "data")


def extract_rows(response: Any) -> list[Any]:
    if isinstance(response, list):
        return response
    if not isinstance(response, dict):
        return []

    for key in LIST_KEYS:
        value = response.get(key)
        if isinstance(value, list):
            return value

    for key in ROOT_KEYS:
        if key in response:
            rows = extract_rows(response[key])
            if rows:
                return rows
            if response[key] == []:
                return []

    return []
