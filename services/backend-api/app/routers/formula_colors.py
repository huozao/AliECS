"""标准型号色彩：企微智能表格「标准型号0117」的客户标准 Lab 与内控容差，配 T+ 当前有效 BOM 的父件名称。

数据来自 doc-sync 已同步的 external_records，本模块只读，不调用企业微信接口。
"""

from __future__ import annotations

import re
from contextlib import closing
from typing import Any

from fastapi import APIRouter, Depends

from app.core import _conn, require_login, require_permission


router = APIRouter(prefix="/v1/formula", tags=["formula-colors"])

# doc-sync 登记的文档与子表名；换表时只改这两个常量，不要硬编码 source_id。
SOURCE_PROVIDER = "wecom"
SOURCE_PROFILE = "COMPANY_A"
SOURCE_DOCUMENT = "标准型号0117"
SOURCE_SHEET = "标准型号规格&月统计"

F_MODEL = "型号"
F_PARENT_CODE = "父件编码"
F_LAB = ("L*（客户标准）", "a*", "b*")
F_TOLERANCE = ("ΔL*合格（内控）", "Δa*合格", "Δb*合格")

# 智能表格里的容差写成 [下限, 上限]，两位小数，逗号后一个半角空格。
_INTERVAL = re.compile(r"^\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]$")

_SOURCE_SQL = """
SELECT id, last_sync_at
FROM external_sources
WHERE provider = %s AND env_profile = %s AND document_name = %s AND sheet_name = %s
ORDER BY id
LIMIT 1
"""

# tplus_bom_records 按版本累积，同一父件编码有多条历史记录；
# missing_since IS NULL 才是 T+ 当前仍存在的那条，否则会取到已作废的旧名称。
_RECORD_SQL = """
WITH active_bom AS (
    SELECT DISTINCT ON (raw_json->>'Code')
           raw_json->>'Code' AS code,
           raw_json->>'Name' AS name,
           raw_json->>'Version' AS version
    FROM tplus_bom_records
    WHERE missing_since IS NULL AND coalesce(raw_json->>'Code', '') <> ''
    ORDER BY raw_json->>'Code', raw_json->>'UpdateDate' DESC
)
SELECT er.external_record_id, er.normalized_json, b.name, b.version
FROM external_records er
LEFT JOIN active_bom b ON b.code = er.normalized_json->>%s
WHERE er.source_id = %s
ORDER BY er.id
"""


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return "" if value is None else str(value).strip()


def _number(payload: dict[str, Any], key: str) -> float | None:
    raw = _text(payload, key)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _interval(payload: dict[str, Any], key: str) -> list[float] | None:
    """把 "[-0.50, -0.20]" 解析成 [-0.5, -0.2]；写反的区间按左小右大纠正。"""
    matched = _INTERVAL.match(_text(payload, key))
    if not matched:
        return None
    low, high = float(matched.group(1)), float(matched.group(2))
    return [low, high] if low <= high else [high, low]


def _match_status(parent_code: str, parent_name: str | None) -> str:
    if not parent_code:
        return "no_parent_code"
    return "matched" if parent_name else "code_missing"


def _build_item(record_id: str, payload: dict[str, Any], bom_name: str | None, bom_version: str | None) -> dict[str, Any]:
    lab = [_number(payload, key) for key in F_LAB]
    tolerance = [_interval(payload, key) for key in F_TOLERANCE]
    parent_code = _text(payload, F_PARENT_CODE)
    return {
        "record_id": record_id,
        "model": _text(payload, F_MODEL),
        "parent_code": parent_code,
        "parent_name": bom_name or "",
        "bom_version": bom_version or "",
        "match_status": _match_status(parent_code, bom_name),
        "lab": lab if all(value is not None for value in lab) else None,
        "tolerance": tolerance,
        "base_resin": _text(payload, "打样基料"),
        "dosage": _number(payload, "添加比例"),
        "delta_e": _text(payload, "ΔE"),
        "standard_rgb": _text(payload, "标准RGB值"),
        "sheet_version": _text(payload, "版本号"),
        "company": _text(payload, "公司"),
        "usage": _text(payload, "用途"),
        "method": _text(payload, "检测方式"),
    }


@router.get("/colors")
def formula_colors(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("formula.read", user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(_SOURCE_SQL, (SOURCE_PROVIDER, SOURCE_PROFILE, SOURCE_DOCUMENT, SOURCE_SHEET))
            source = cur.fetchone()
            if not source:
                return {
                    "meta": {
                        "document": SOURCE_DOCUMENT,
                        "sheet": SOURCE_SHEET,
                        "available": False,
                        "message": "该智能表格尚未在 doc-sync 登记或同步。",
                    },
                    "items": [],
                }
            source_id, last_sync_at = source[0], source[1]
            cur.execute(_RECORD_SQL, (F_PARENT_CODE, source_id))
            rows = cur.fetchall()

    items = [_build_item(str(row[0]), row[1] or {}, row[2], row[3]) for row in rows]
    with_lab = [item for item in items if item["lab"]]
    return {
        "meta": {
            "document": SOURCE_DOCUMENT,
            "sheet": SOURCE_SHEET,
            "available": True,
            "source_id": source_id,
            "last_sync_at": last_sync_at.isoformat() if last_sync_at else None,
            "total_records": len(items),
            "with_lab": len(with_lab),
            "code_missing": sum(1 for item in items if item["match_status"] == "code_missing"),
        },
        # 三维视图只用得上有完整 Lab 的行，其余行留在统计里即可。
        "items": with_lab,
    }
