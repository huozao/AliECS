from __future__ import annotations


def writeback_guard(stage_records: list[dict[str, str]]) -> bool:
    """阶段一：回写保护默认关闭。"""
    _ = stage_records
    return False
