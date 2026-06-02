from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PARENT_COLUMNS = [
    "父件编码",
    "父件名称",
    "规格型号",
    "版本号",
    "计量单位",
    "生产数量",
    "生产车间编码",
    "生产车间",
    "预入仓库编码",
    "预入仓库",
    "默认BOM",
    "成品率%",
    "停用",
    "创建时间",
]

CHILD_COLUMNS = [
    "版本号",
    "父件编码",
    "子件编码",
    "子件名称",
    "规格型号",
    "计量单位",
    "需用数量",
    "标准用量",
    "存货图片",
    "备注",
    "子件BOM",
    "子件默认BOM",
    "生产数量",
    "损耗率%",
    "倒冲料",
    "预出仓库编码",
    "预出仓库",
    "材料倒冲方式",
]


def transform_bom_rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [_flatten_mapping(row) if isinstance(row, Mapping) else {"value": row} for row in rows]


def transform_bom_workbook_rows(rows: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parent_rows: list[dict[str, Any]] = []
    child_rows: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, Mapping):
            continue

        parent_row = {
            "父件编码": row.get("Code"),
            "父件名称": row.get("Name"),
            "规格型号": row.get("Specification"),
            "版本号": row.get("Version"),
            "计量单位": _nested_value(row, "Unit", "Name"),
            "生产数量": row.get("ProduceQuantity"),
            "生产车间编码": _nested_value(row, "Manufactureplant", "Code"),
            "生产车间": _nested_value(row, "Manufactureplant", "Name"),
            "预入仓库编码": _nested_value(row, "Warehouse", "Code"),
            "预入仓库": _nested_value(row, "Warehouse", "Name"),
            "默认BOM": _boolish_to_flag(row.get("IsDefaultBom")),
            "成品率%": row.get("YieldRate"),
            "停用": _boolish_to_flag(row.get("Disabled")),
            "创建时间": row.get("CreateDate"),
        }
        parent_rows.append(parent_row)

        children = row.get("BOMChilds") or []
        if not isinstance(children, list):
            continue

        for child in children:
            if not isinstance(child, Mapping):
                continue
            child_rows.append(
                {
                    "版本号": row.get("Version"),
                    "父件编码": row.get("Code"),
                    "子件编码": child.get("Code"),
                    "子件名称": child.get("Name"),
                    "规格型号": child.get("Specification"),
                    "计量单位": _nested_value(child, "Unit", "Name"),
                    "需用数量": child.get("RequiredQuantity"),
                    "标准用量": child.get("RequiredQuantity"),
                    "存货图片": None,
                    "备注": child.get("Memo"),
                    "子件BOM": child.get("ChildBOM"),
                    "子件默认BOM": _boolish_to_flag(_nested_value(child, "ChildBOM", "IsDefaultBom")),
                    "生产数量": row.get("ProduceQuantity"),
                    "损耗率%": child.get("WasteRate"),
                    "倒冲料": _boolish_to_flag(child.get("BackflushMaterial")),
                    "预出仓库编码": _nested_value(child, "Warehouse", "Code"),
                    "预出仓库": _nested_value(child, "Warehouse", "Name"),
                    "材料倒冲方式": child.get("BackflushMaterialMethod"),
                }
            )

    return parent_rows, child_rows


def _flatten_mapping(row: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in row.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            flattened.update(_flatten_mapping(value, full_key))
        else:
            flattened[full_key] = value
    return flattened


def _nested_value(row: Mapping[str, Any], key: str, nested_key: str) -> Any:
    value = row.get(key)
    if isinstance(value, Mapping):
        return value.get(nested_key)
    return None


def _boolish_to_flag(value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return 1
        if lowered == "false":
            return 0
    return value
