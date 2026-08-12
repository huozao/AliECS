from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.recipes.bom_query import locate_recipe_source


ERROR_KIND_LABELS = {
    "auth": "凭据过期",
    "rate_limit": "请求限流",
    "network": "网络异常",
    "schema": "数据结构变化",
    "write": "写入失败",
    "unknown": "未知错误",
}


def error_kind_label(error_kind: str | None) -> str:
    return ERROR_KIND_LABELS.get(str(error_kind or "unknown"), "未知错误")


def classify_freshness(
    last_success_at,
    sla_seconds,
    *,
    now=None,
) -> dict[str, Any]:
    if sla_seconds is None:
        return {
            "state": "unmonitored",
            "sla_seconds": None,
            "age_seconds": None,
            "ratio": None,
        }

    sla = int(sla_seconds)
    if last_success_at is None:
        return {
            "state": "never",
            "sla_seconds": sla,
            "age_seconds": None,
            "ratio": None,
        }

    current = now or datetime.now(timezone.utc)
    age = max(0, int((current - last_success_at).total_seconds()))
    ratio = age / sla if sla > 0 else None
    state = "stale" if age > sla else ("warning" if age >= sla * 0.8 else "fresh")
    return {
        "state": state,
        "sla_seconds": sla,
        "age_seconds": age,
        "ratio": ratio,
    }


def formula_bom_artifact() -> dict[str, Any] | None:
    try:
        path = locate_recipe_source()
        stat = path.stat()
    except (FileNotFoundError, OSError):
        return None

    return {
        "name": path.name,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "mtime_epoch": int(stat.st_mtime),
    }
