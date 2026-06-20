from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any


def relation_field_specs(table_ids: dict[str, str]) -> list[tuple[str, str, str]]:
    session = table_ids["会话索引表"]
    message = table_ids["消息日志表"]
    group = table_ids["群表"]
    user = table_ids["用户表"]
    return [
        ("会话索引表", "关联用户记录", user),
        ("会话索引表", "关联群记录", group),
        ("消息日志表", "关联用户记录", user),
        ("消息日志表", "关联群记录", group),
        ("消息日志表", "匹配会话记录", session),
        ("回复任务表", "关联消息记录", message),
        ("回复任务表", "关联会话记录", session),
        ("群表", "默认会话记录", session),
        ("用户表", "默认私聊会话记录", session),
        ("规则配置表", "关联用户记录", user),
        ("规则配置表", "关联群记录", group),
        ("规则配置表", "关联会话记录", session),
    ]


def control_select_field_specs() -> list[tuple[str, str, tuple[str, ...]]]:
    return [
        ("会话索引表", "会话类型", ("私聊", "群聊")),
        ("会话索引表", "会话状态", ("待创建", "活跃", "已归档")),
        ("消息日志表", "聊天类型", ("私聊", "群聊")),
        ("消息日志表", "命令类型", ("无", "/新对话", "/重置", "/摘要")),
        ("消息日志表", "处理状态", ("已回复", "仅记录", "失败")),
        ("回复任务表", "任务类型", ("新建会话", "重置会话", "总结会话", "普通回复")),
        ("回复任务表", "任务状态", ("待处理", "处理中", "已发送", "失败", "已取消")),
        ("回复任务表", "审核状态", ("无需审核", "待审核", "已通过", "已拒绝")),
        ("群表", "群类型", ("普通群", "外部群", "临时群")),
        ("群表", "回复模式", ("回复所有", "仅@回复")),
        ("群表", "风险级别", ("低", "中", "高")),
        ("用户表", "用户状态", ("启用", "停用")),
        ("用户表", "用户角色", ("普通用户", "管理员")),
        ("规则配置表", "规则对象类型", ("全局", "用户", "群", "会话")),
        ("规则配置表", "回复模式", ("回复所有", "仅@回复")),
    ]


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(_field_text(item) for item in value).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            text = _field_text(value.get(key))
            if text:
                return text
        return ""
    return str(value).strip()


CONTROL_FIELD_VALUE_ALIASES: dict[tuple[str, str], dict[str, str]] = {
    ("群表", "回复模式"): {"全部回复": "回复所有"},
    ("规则配置表", "回复模式"): {"全部回复": "回复所有"},
}


def canonical_control_field_value(table_name: str, field_name: str, value: Any) -> str:
    text = _field_text(value)
    return CONTROL_FIELD_VALUE_ALIASES.get((table_name, field_name), {}).get(text, text)


def validate_control_field_values(
    table_name: str,
    field_name: str,
    options: tuple[str, ...],
    records: list[dict[str, Any]],
) -> None:
    allowed = set(options)
    invalid: list[str] = []
    for record in records:
        original = _field_text((record.get("fields") or {}).get(field_name))
        canonical = canonical_control_field_value(table_name, field_name, original)
        if canonical and canonical not in allowed:
            invalid.append(original)
    invalid = sorted(set(invalid))
    if invalid:
        raise ValueError(f"{table_name}.{field_name} contains unsupported values: {', '.join(invalid)}")


def single_select_field_payload(field_name: str, options: tuple[str, ...]) -> dict[str, Any]:
    return {
        "field_name": field_name,
        "type": 3,
        "property": {
            "options": [
                {"name": option, "color": index % 16}
                for index, option in enumerate(options)
            ]
        },
    }


def is_default_reply_rule_id(rule_id: str) -> bool:
    return rule_id == "global-default" or rule_id.startswith("group-default-")


def _has_value(value: Any) -> bool:
    if value in (None, "", False):
        return False
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_value(item) for item in value)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-token", required=True)
    parser.add_argument("--delete-empty-default-table", action="store_true")
    parser.add_argument("--convert-control-fields-to-select", action="store_true")
    parser.add_argument("--set-default-reply-all", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import openclaw_bridge as bridge

    os.environ["FEISHU_SESSION_CONSOLE_APP_TOKEN"] = args.app_token
    app_token = urllib.parse.quote(args.app_token)
    response = bridge.feishu_get_json(f"/bitable/v1/apps/{app_token}/tables?page_size=100")
    tables = {
        str(item.get("name") or ""): str(item.get("table_id") or "")
        for item in (response.get("data") or {}).get("items", [])
    }
    required = {"会话索引表", "消息日志表", "回复任务表", "群表", "用户表", "规则配置表"}
    missing = sorted(required - tables.keys())
    if missing:
        raise RuntimeError("missing tables: " + ", ".join(missing))

    created: list[str] = []
    for table_name, field_name, linked_table_id in relation_field_specs(tables):
        table_id = tables[table_name]
        fields_response = bridge.feishu_get_json(
            f"/bitable/v1/apps/{app_token}/tables/{urllib.parse.quote(table_id)}/fields?page_size=500"
        )
        existing = {
            str(item.get("field_name") or ""): item
            for item in (fields_response.get("data") or {}).get("items", [])
        }
        if field_name in existing:
            if int(existing[field_name].get("type") or 0) != 18:
                raise RuntimeError(f"{table_name}.{field_name} exists with non-link type")
            continue
        bridge.feishu_post_json(
            f"/bitable/v1/apps/{app_token}/tables/{urllib.parse.quote(table_id)}/fields",
            {
                "field_name": field_name,
                "type": 18,
                "property": {"table_id": linked_table_id, "multiple": False},
            },
        )
        created.append(f"{table_name}.{field_name}")

    converted: list[str] = []
    normalized_values: list[str] = []
    if args.convert_control_fields_to_select:
        fields_by_table: dict[str, dict[str, dict[str, Any]]] = {}
        records_by_table: dict[str, list[dict[str, Any]]] = {}
        pending_normalizations: list[tuple[str, str, str, str]] = []
        for table_name, field_name, options in control_select_field_specs():
            table_id = tables[table_name]
            if table_name not in fields_by_table:
                fields_response = bridge.feishu_get_json(
                    f"/bitable/v1/apps/{app_token}/tables/{urllib.parse.quote(table_id)}/fields?page_size=500"
                )
                fields_by_table[table_name] = {
                    str(item.get("field_name") or ""): item
                    for item in (fields_response.get("data") or {}).get("items", [])
                }
                records_by_table[table_name] = bridge.list_feishu_bitable_records(table_id)
            field = fields_by_table[table_name].get(field_name)
            if not field:
                raise RuntimeError(f"missing field: {table_name}.{field_name}")
            field_type = int(field.get("type") or 0)
            if field_type not in {1, 3}:
                raise RuntimeError(f"{table_name}.{field_name} has unsupported type {field_type}")
            validate_control_field_values(table_name, field_name, options, records_by_table[table_name])
            for record in records_by_table[table_name]:
                record_id = str(record.get("record_id") or "")
                original = _field_text((record.get("fields") or {}).get(field_name))
                canonical = canonical_control_field_value(table_name, field_name, original)
                if record_id and original and canonical != original:
                    pending_normalizations.append((table_id, record_id, field_name, canonical))

        for table_id, record_id, field_name, canonical in pending_normalizations:
            bridge.update_feishu_bitable_record(table_id, record_id, {field_name: canonical})
            normalized_values.append(f"{table_id}.{record_id}.{field_name}")

        for table_name, field_name, options in control_select_field_specs():
            table_id = tables[table_name]
            field = fields_by_table[table_name][field_name]
            existing_options = tuple(
                str(item.get("name") or "")
                for item in (field.get("property") or {}).get("options", [])
            )
            if int(field.get("type") or 0) == 3 and existing_options == options:
                continue
            field_id = str(field.get("field_id") or "")
            if not field_id:
                raise RuntimeError(f"missing field_id: {table_name}.{field_name}")
            bridge.feishu_request_json(
                (
                    f"/bitable/v1/apps/{app_token}/tables/{urllib.parse.quote(table_id)}/fields/"
                    f"{urllib.parse.quote(field_id)}"
                ),
                single_select_field_payload(field_name, options),
                method="PUT",
            )
            converted.append(f"{table_name}.{field_name}")

    default_reply_updates: list[str] = []
    if args.set_default_reply_all:
        for record in bridge.list_feishu_bitable_records(tables["群表"]):
            record_id = str(record.get("record_id") or "")
            if record_id:
                bridge.update_feishu_bitable_record(tables["群表"], record_id, {"回复模式": "回复所有"})
                default_reply_updates.append(f"群表.{record_id}")
        for record in bridge.list_feishu_bitable_records(tables["规则配置表"]):
            fields = record.get("fields") or {}
            if not is_default_reply_rule_id(_field_text(fields.get("规则编号"))):
                continue
            record_id = str(record.get("record_id") or "")
            if record_id:
                bridge.update_feishu_bitable_record(tables["规则配置表"], record_id, {"回复模式": "回复所有"})
                default_reply_updates.append(f"规则配置表.{record_id}")

    deleted_default = False
    default_table_id = tables.get("数据表", "")
    if args.delete_empty_default_table and default_table_id:
        records = bridge.list_feishu_bitable_records(default_table_id)
        if all(not _has_value(record.get("fields") or {}) for record in records):
            bridge.feishu_request_json(
                f"/bitable/v1/apps/{app_token}/tables/{urllib.parse.quote(default_table_id)}",
                None,
                method="DELETE",
            )
            deleted_default = True

    print(
        json.dumps(
            {
                "created_fields": created,
                "converted_fields": converted,
                "normalized_values": normalized_values,
                "default_reply_updates": default_reply_updates,
                "deleted_empty_default_table": deleted_default,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
