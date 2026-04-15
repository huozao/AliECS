from __future__ import annotations


def writeback_guard(stage_records: list[dict[str, str]]) -> bool:
    """Guardrail for writeback; Phase 1 keeps disabled by default."""
    return False
