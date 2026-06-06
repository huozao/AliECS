from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.recipes.bom_query import locate_recipe_source


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


def active_bom_dir() -> Path:
    path = Path(os.getenv("RECIPE_ACTIVE_BOM_DIR", "/app/recipe-active-bom"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def active_bom_filename(timestamp: str | None = None) -> str:
    return f"bom_{timestamp or datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"


def export_active_bom_rows(rows: list[Any], timestamp: str | None = None) -> dict[str, str]:
    target = active_bom_dir() / active_bom_filename(timestamp)
    parent_rows, child_rows = _transform_bom_workbook_rows(rows)
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        pd.DataFrame(parent_rows, columns=PARENT_COLUMNS).to_excel(writer, sheet_name="物料清单", index=False)
        pd.DataFrame(child_rows, columns=CHILD_COLUMNS).to_excel(writer, sheet_name="子件明细", index=False)
    return {"active_export_name": target.name, "active_export_path": str(target), "active_export_source": "snapshot_records"}


def copy_latest_bom_source(timestamp: str | None = None) -> dict[str, str]:
    source = locate_recipe_source(include_active=False)
    target = active_bom_dir() / active_bom_filename(timestamp)
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return {
        "active_export_name": target.name,
        "active_export_path": str(target),
        "active_export_source": source.name,
    }


def _transform_bom_workbook_rows(rows: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parent_rows: list[dict[str, Any]] = []
    child_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        parent_rows.append(
            {
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
        )
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
