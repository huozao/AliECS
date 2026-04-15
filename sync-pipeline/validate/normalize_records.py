from __future__ import annotations


def normalize_records(raw_records: list[dict[str, str]]) -> list[dict[str, str]]:
    """Normalize source records into stage-shape records."""
    normalized: list[dict[str, str]] = []
    for row in raw_records:
        normalized.append({
            "external_id": row.get("external_id", ""),
            "name": row.get("name", "").strip(),
        })
    return normalized
