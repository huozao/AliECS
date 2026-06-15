from __future__ import annotations

from typing import Any


CONTACT_SHEET_CHANNELS = {
    "微信用户清单": "wechat",
    "飞书用户清单": "feishu",
}

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
    channel = CONTACT_SHEET_CHANNELS.get(str(sheet_name or "").strip())
    if not channel:
        return None
    peer_id = _field(row, "peer_id")
    if not peer_id:
        return None
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


def parse_enabled(value: Any) -> bool:
    if value is None or value == "":
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"否", "不", "false", "0", "no", "n", "disabled", "disable", "off"}:
        return False
    return True


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


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
