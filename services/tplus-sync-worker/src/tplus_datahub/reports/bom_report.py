from __future__ import annotations

from typing import Any


def build_bom_summary(rows: list[Any]) -> dict[str, int]:
    return {"row_count": len(rows)}
