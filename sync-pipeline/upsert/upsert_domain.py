from __future__ import annotations


def upsert_domain(stage_records: list[dict[str, str]]) -> int:
    """Phase 1 no-op upsert placeholder."""
    return len(stage_records)
