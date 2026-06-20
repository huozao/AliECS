from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FeishuMention:
    open_id: str
    name: str = ""
    user_id: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> "FeishuMention":
        if not isinstance(value, dict):
            return cls(open_id="")
        ident = value.get("id") if isinstance(value.get("id"), dict) else value
        open_id = ""
        user_id = ""
        if isinstance(ident, dict):
            open_id = str(ident.get("open_id") or ident.get("openId") or "")
            user_id = str(ident.get("user_id") or ident.get("userId") or "")
        return cls(
            open_id=open_id,
            name=str(value.get("name") or ""),
            user_id=user_id,
        )


@dataclass(frozen=True)
class FeishuEvent:
    schema_version: str
    event_id: str
    event_type: str
    tenant_key: str
    create_time: str
    sender_open_id: str
    sender_user_id: str
    sender_name: str
    chat_id: str
    chat_type: str
    message_id: str
    message_type: str
    content: str
    mentions: tuple[FeishuMention, ...]
    raw: dict[str, Any]

    @property
    def is_group_message(self) -> bool:
        return self.chat_type == "group"

    @property
    def is_direct_message(self) -> bool:
        return self.chat_type == "p2p"

    def mentions_open_id(self, target_open_id: str) -> bool:
        if not target_open_id:
            return False
        return any(m.open_id == target_open_id for m in self.mentions)


def parse_feishu_event(payload: dict[str, Any]) -> FeishuEvent:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}

    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}

    message = event.get("message") if isinstance(event.get("message"), dict) else {}

    mentions_raw = message.get("mentions") if isinstance(message.get("mentions"), list) else []
    mentions = tuple(FeishuMention.from_dict(item) for item in mentions_raw)

    return FeishuEvent(
        schema_version=str(payload.get("schema") or ""),
        event_id=str(header.get("event_id") or ""),
        event_type=str(header.get("event_type") or ""),
        tenant_key=str(header.get("tenant_key") or ""),
        create_time=str(header.get("create_time") or ""),
        sender_open_id=str(sender_id.get("open_id") or sender.get("open_id") or ""),
        sender_user_id=str(sender_id.get("user_id") or sender.get("user_id") or ""),
        sender_name=str(sender.get("sender_name") or sender.get("name") or ""),
        chat_id=str(message.get("chat_id") or ""),
        chat_type=str(message.get("chat_type") or ""),
        message_id=str(message.get("message_id") or ""),
        message_type=str(message.get("message_type") or ""),
        content=str(message.get("content") or ""),
        mentions=mentions,
        raw=payload,
    )
