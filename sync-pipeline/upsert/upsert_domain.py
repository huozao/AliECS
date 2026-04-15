from __future__ import annotations


def upsert_domain(stage_records: list[dict[str, str]]) -> int:
    """阶段一：写入更新占位逻辑，返回记录数。"""
    return len(stage_records)
