from __future__ import annotations

from typing import Any


CONTACT_SHEET_CHANNELS = {
    "微信用户清单": "wechat",
    "飞书用户清单": "feishu",
}

SESSION_INDEX_SHEETS = {"会话索引表", "sessions", "飞书会话索引表"}
SESSION_ACTIVE_STATUSES = {"活跃", "待创建", "active", "pending", "pending_create"}

FIELD_ALIASES = {
    "peer_id": ("peer_id", "Peer ID", "用户ID", "渠道用户ID", "微信ID", "微信peer", "飞书open_id", "open_id"),
    "display_name": ("display_name", "昵称", "显示名", "用户名", "姓名", "微信名称", "飞书名称"),
    "remark": ("remark", "备注", "真名"),
    "enabled": ("enabled", "启用", "是否启用", "权限开关", "权限"),
    "project_url": ("project_url", "ChatGPT项目地址", "项目地址", "project", "新对话链接"),
    "project_name": ("project_name", "项目名称", "ChatGPT项目名", "所属项目名称"),
    "tags": ("tags", "标签", "分组"),
    "daily_quota": ("daily_quota", "每日配额", "配额"),
    "notes": ("notes", "说明", "备注说明"),
}

SESSION_FIELD_ALIASES = {
    "session_key": ("session_key", "会话key", "会话键", "会话 Key"),
    "session_type": ("会话类型", "session_type"),
    "display_name": ("会话名称", "飞书用户名", "飞书群名", "display_name", "用户名", "群名称"),
    "project_url": ("ChatGPT 项目首页链接", "ChatGPT项目首页链接", "项目首页链接", "project_url"),
    "conversation_url": ("ChatGPT 对话链接", "ChatGPT对话链接", "新对话链接", "conversation_url"),
    "project_name": ("ChatGPT 项目名", "ChatGPT项目名", "project_name"),
    "status": ("会话状态", "status"),
    "is_current": ("是否当前会话", "is_current", "当前会话"),
    "notes": ("备注", "notes"),
}


def sync_managed_contacts_from_sheet(store: Any, sheet_name: str, rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        if sync_managed_contact_from_row(store, sheet_name, row):
            count += 1
    return count


def sync_managed_contact_from_row(store: Any, sheet_name: str, row: dict[str, Any]) -> bool:
    contact = normalize_contact_row(sheet_name, row)
    if not contact or not hasattr(store, "upsert_managed_contact"):
        return False
    store.upsert_managed_contact(contact)
    return True


def normalize_contact_row(sheet_name: str, row: dict[str, Any]) -> dict[str, Any] | None:
    normalized_sheet = str(sheet_name or "").strip()
    if normalized_sheet in SESSION_INDEX_SHEETS:
        return normalize_session_index_row(normalized_sheet, row)

    channel = CONTACT_SHEET_CHANNELS.get(normalized_sheet)
    if not channel:
        return None
    peer_id = _field(row, "peer_id")
    if not peer_id:
        return None
    if channel == "feishu":
        peer_id = _canonical_feishu_user_peer(peer_id)
    daily_quota = _field(row, "daily_quota")
    return {
        "channel": channel,
        "peer_id": peer_id,
        "display_name": _field(row, "display_name"),
        "remark": _field(row, "remark"),
        "enabled": parse_enabled(_field(row, "enabled")),
        "project_url": _field(row, "project_url"),
        "project_name": _field(row, "project_name"),
        "tags": _field(row, "tags"),
        "daily_quota": _int_or_none(daily_quota),
        "notes": _field(row, "notes"),
        "source_sheet": sheet_name,
    }


def normalize_session_index_row(sheet_name: str, row: dict[str, Any]) -> dict[str, Any] | None:
    status = _session_field(row, "status")
    if not status or status.strip().lower() not in SESSION_ACTIVE_STATUSES:
        return None
    if not parse_truthy(_session_field(row, "is_current")):
        return None

    session_key = _session_field(row, "session_key")
    peer_id = _peer_from_session_key(session_key)
    if not peer_id:
        return None

    project_url = _project_home_url(row)
    notes = _session_field(row, "notes")
    if project_url and "/c/" in project_url and "needs_project_home_url" not in notes:
        notes = f"{notes}; needs_project_home_url".strip("; ")

    return {
        "channel": "feishu",
        "peer_id": peer_id,
        "display_name": _session_field(row, "display_name"),
        "remark": "",
        "enabled": True,
        "project_url": project_url,
        "project_name": _session_field(row, "project_name"),
        "tags": _session_field(row, "session_type"),
        "daily_quota": None,
        "notes": notes,
        "source_sheet": sheet_name,
    }


def _peer_from_session_key(session_key: str) -> str:
    parts = [part.strip() for part in str(session_key or "").split(":") if part.strip()]
    if len(parts) < 3:
        return ""
    kind = parts[1]
    if kind == "user":
        return _canonical_feishu_user_peer(parts[2])
    if kind == "group":
        return _canonical_feishu_group_peer(parts[2])
    if kind == "group_user" and len(parts) >= 4:
        chat_id = _strip_feishu_peer_prefix(parts[2])
        open_id = _strip_feishu_peer_prefix(parts[3])
        return f"group_user:{chat_id}:{open_id}" if chat_id and open_id else ""
    return ""


def _canonical_feishu_user_peer(value: str) -> str:
    peer = _strip_feishu_peer_prefix(value)
    return f"user:{peer}" if peer else ""


def _canonical_feishu_group_peer(value: str) -> str:
    peer = _strip_feishu_peer_prefix(value)
    return f"group:{peer}" if peer else ""


def _strip_feishu_peer_prefix(value: str) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    for prefix in ("user:", "group:", "chat:", "open_id:", "openid:"):
        if lowered.startswith(prefix):
            return text[len(prefix):]
    return text


def _project_home_url(row: dict[str, Any]) -> str:
    project_url = _session_field(row, "project_url")
    if project_url:
        return project_url
    conversation_url = _session_field(row, "conversation_url")
    marker = "/c/"
    if "chatgpt.com/g/" in conversation_url and marker in conversation_url:
        return conversation_url.split(marker, 1)[0].rstrip("/") + "/project"
    return conversation_url


def parse_enabled(value: Any) -> bool:
    if value is None or value == "":
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"否", "不", "false", "0", "no", "n", "disabled", "disable", "off"}:
        return False
    return True


def parse_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"是", "true", "1", "yes", "y", "on", "✓", "当前", "current"}


def _field(row: dict[str, Any], field: str) -> str:
    aliases = FIELD_ALIASES[field]
    for alias in aliases:
        if alias in row and row[alias] not in (None, ""):
            return str(row[alias]).strip()
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for alias in aliases:
        value = lowered.get(alias.lower())
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _session_field(row: dict[str, Any], field: str) -> str:
    return _field_from_aliases(row, SESSION_FIELD_ALIASES[field])


def _field_from_aliases(row: dict[str, Any], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        if alias in row and row[alias] not in (None, ""):
            return str(row[alias]).strip()
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for alias in aliases:
        value = lowered.get(alias.lower())
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
