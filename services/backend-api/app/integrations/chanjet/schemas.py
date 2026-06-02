from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChanjetEvent:
    event_id: str | None
    msg_type: str | None
    app_key: str | None
    app_id: str | None
    received_time: str | None
    biz_content: dict[str, Any]
    raw: dict[str, Any]


def parse_chanjet_event(payload: dict[str, Any]) -> ChanjetEvent:
    biz_content = payload.get("bizContent")
    if not isinstance(biz_content, dict):
        biz_content = {}

    return ChanjetEvent(
        event_id=_to_optional_str(payload.get("id")),
        msg_type=_to_optional_str(payload.get("msgType")),
        app_key=_to_optional_str(payload.get("appKey")),
        app_id=_to_optional_str(payload.get("appId")),
        received_time=_to_optional_str(payload.get("time")),
        biz_content=biz_content,
        raw=payload,
    )


def _to_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
