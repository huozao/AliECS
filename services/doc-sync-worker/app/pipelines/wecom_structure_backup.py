"""企微智能表格的通用读写工具。

⚠️ 本文件曾经是「企微智能表格结构备份」流水线（逐文档逐工作表复制字段结构到
「企微A/B-最新结构」「飞书-最新结构」「结构变更历史」四张表）。该流水线于
2026-08-14 被 PR #313 从 worker 循环摘除，2026-08-19 正式下线并删除实现，
唯一有效的定位备份是 `document_locator_mirror` 写的「文档定位档案」两张表。
下线原委、四张旧表的空行成因见 `docs/constraints/doc-sync.md`。

文件名和 `structure_backup_enabled` 保持原样不改名：后者对应生产环境变量
`WECOM_STRUCTURE_BACKUP_ENABLED`，改名等于改生产配置。现在它是定位档案镜像的总开关。
"""

from __future__ import annotations

import os
from typing import Any


FIELD_BATCH_SIZE = 20


def _sheet_identity(sheet: dict[str, Any]) -> tuple[str, str]:
    properties = sheet.get("properties") if isinstance(sheet.get("properties"), dict) else {}
    sheet_id = str(sheet.get("sheet_id") or sheet.get("id") or "")
    title = str(properties.get("title") or sheet.get("title") or sheet.get("name") or "")
    return sheet_id, title


def _field_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    fields = response.get("fields") or response.get("field_list") or []
    return fields if isinstance(fields, list) else []


def _initialize_sheet_fields(
    client: Any,
    docid: str,
    sheet_id: str,
    target_titles: list[str],
) -> None:
    existing = _field_items(client.get_fields(docid, sheet_id))
    remaining = list(target_titles)
    if existing and remaining:
        first = existing[0]
        first_id = str(first.get("field_id") or first.get("id") or "")
        first_type = str(first.get("field_type") or first.get("type") or "FIELD_TYPE_TEXT")
        if first_id and str(first.get("field_title") or first.get("title") or "") != remaining[0]:
            client.update_fields(
                docid,
                sheet_id,
                [{"field_id": first_id, "field_title": remaining[0], "field_type": first_type}],
            )
        remaining = remaining[1:]
    field_defs = [{"field_title": name, "field_type": "FIELD_TYPE_TEXT"} for name in remaining]
    # 每批内部 reversed 是因为 add_fields 把字段插在首列之后而不是追加到末尾；
    # 同理**批与批之间也必须逆序**，否则后一批会插到前一批前面。字段数 ≤21 时只有
    # 一批，这个错位不会显形——2026-08-19 定位档案加到 26 列才暴露出来。
    batches = [
        field_defs[start : start + FIELD_BATCH_SIZE]
        for start in range(0, len(field_defs), FIELD_BATCH_SIZE)
    ]
    for batch in reversed(batches):
        client.add_fields(docid, sheet_id, list(reversed(batch)))


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        texts = [_cell_text(item) for item in value]
        return "; ".join(item for item in texts if item)
    if isinstance(value, dict):
        for key in ("text", "value", "name"):
            if key in value:
                return str(value.get(key) or "")
        return ""
    return str(value)


def _record_index(client: Any, docid: str, sheet_id: str, key_title: str) -> dict[str, dict[str, Any]]:
    fields = _field_items(client.get_fields(docid, sheet_id))
    title_to_id = {
        str(field.get("field_title") or field.get("title") or field.get("name") or ""): str(
            field.get("field_id") or field.get("id") or ""
        )
        for field in fields
    }
    key_id = title_to_id.get(key_title, "")
    response = client.get_records(docid, sheet_id)
    records = response.get("records") or []
    indexed: dict[str, dict[str, Any]] = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        values = record.get("values") if isinstance(record.get("values"), dict) else {}
        key = _cell_text(values.get(key_title) if key_title in values else values.get(key_id))
        if key:
            normalized = {
                title: _cell_text(values.get(title) if title in values else values.get(field_id))
                for title, field_id in title_to_id.items()
            }
            indexed[key] = {**record, "title_values": normalized}
    return indexed


def structure_backup_enabled() -> bool:
    return str(os.getenv("WECOM_STRUCTURE_BACKUP_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def structure_backup_admin_users() -> list[str]:
    raw = str(os.getenv("WECOM_DOC_ADMIN_USERS", ""))
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
