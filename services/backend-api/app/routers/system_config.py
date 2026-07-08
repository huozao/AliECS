"""系统配置生效总览：聚合各域当前值、来源、同步时间与编辑入口。"""

from __future__ import annotations

import os

from contextlib import closing
from typing import Any

from fastapi import APIRouter, Depends

from app.core import DEFAULT_FEATURES, _conn, require_admin
from app.routers import exports
from app.routers.ops import _doc_sync_config_response, _read_doc_sync_config_row


router = APIRouter()


def _feishu_table_url(table_env_name: str) -> str | None:
    app_token = os.getenv("FEISHU_SYSTEM_CONFIG_APP_TOKEN", "").strip()
    table_id = os.getenv(table_env_name, "").strip()
    if not app_token or not table_id:
        return None
    return f"https://cloud.feishu.cn/base/{app_token}?table={table_id}"


def _latest_system_config_sync_at(sheet_name: str) -> str | None:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT MAX(er.synced_at)
                    FROM external_sources es
                    JOIN external_records er ON er.source_id = es.id
                    WHERE es.provider = 'feishu'
                      AND es.document_name = '系统配置'
                      AND es.sheet_name = %s
                      AND es.status = 'active'
                    """,
                    (sheet_name,),
                )
                row = cur.fetchone()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    return str(row[0])


def _database_available() -> bool:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "开启" if value else "关闭"
    if isinstance(value, (set, tuple, list)):
        return "、".join(str(item) for item in sorted(value)) if value else "空"
    if value is None or value == "":
        return "未设置"
    return str(value)


def _row(key: str, label: str, value: Any, source: str, note: str | None = None) -> dict[str, Any]:
    return {"key": key, "label": label, "value": _value(value), "source": source, "note": note}


def _domain(
    domain: str,
    title: str,
    editor_label: str,
    editor_url: str | None,
    source: str,
    rows: list[dict[str, Any]],
    last_synced_at: str | None = None,
    status: str = "ok",
    emergency: dict[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "domain": domain,
        "title": title,
        "editor": {"label": editor_label, "url": editor_url},
        "last_synced_at": last_synced_at,
        "source": source,
        "status": status,
        "rows": rows,
        "emergency": emergency or {"pause_supported": False, "override_supported": False},
        "note": note,
    }


def _doc_sync_source(row: dict[str, Any]) -> str:
    updated_by = str(row.get("updated_by") or "")
    if updated_by in {"feishu-system-config-table", "feishu-config-table"}:
        return "系统配置镜像"
    if updated_by:
        return "手动覆盖"
    return "默认/回退"


def _doc_sync_domain() -> dict[str, Any]:
    row = _read_doc_sync_config_row()
    config = _doc_sync_config_response(row)
    source = _doc_sync_source(row)
    write_available = _database_available()
    return _domain(
        "doc_sync",
        "文档同步",
        "飞书系统配置 / 同步配置",
        _feishu_table_url("FEISHU_SYSTEM_CONFIG_DOC_SYNC_TABLE_ID"),
        source,
        [
            _row("doc_sync.enabled", "启用拉取", config["enabled"], source),
            _row("doc_sync.schedule", "同步周期", f'{config["interval_hours"]} 小时', source),
            _row("doc_sync.anchor_time", "起点时间", config["anchor_time"] or "不锚定", source),
            _row("doc_sync.pull_paused", "暂停表格拉取", config["pull_paused"], "应急开关"),
        ],
        last_synced_at=_latest_system_config_sync_at("同步配置"),
        status="warn" if source != "系统配置镜像" else "ok",
        emergency={
            "pause_supported": write_available,
            "override_supported": False,
            "write_available": write_available,
            "pull_paused": config["pull_paused"],
            "enabled": config["enabled"],
            "interval_hours": config["interval_hours"],
            "anchor_time": config["anchor_time"],
        },
    )


def _chat_mode_domain() -> dict[str, Any]:
    record = exports._system_config_record("对话模式")
    value = exports._config_text(record.get("对话模式默认")) if record else ""
    source = "系统配置镜像" if value else "默认/回退"
    return _domain(
        "chat_mode",
        "对话模式",
        "飞书系统配置 / 对话模式",
        _feishu_table_url("FEISHU_SYSTEM_CONFIG_CHAT_MODE_TABLE_ID"),
        source,
        [_row("chat_mode.default", "默认对话模式", value or "高级", source)],
        last_synced_at=_latest_system_config_sync_at("对话模式"),
        status="ok" if value else "warn",
        note="bridge 实时直读飞书；此处展示 doc-sync 观察镜像，可能按同步周期滞后。",
    )


def _tplus_export_domain() -> dict[str, Any]:
    record = exports._system_config_record("T+导出说明")
    configured = [key for key, value in record.items() if key != "配置编号" and exports._config_text(value)]
    source = "系统配置镜像" if configured else "代码默认"
    return _domain(
        "tplus_export",
        "T+导出说明",
        "飞书系统配置 / T+导出说明",
        _feishu_table_url("FEISHU_SYSTEM_CONFIG_TPLUS_EXPORT_TABLE_ID"),
        source,
        [
            _row("tplus_export.configured_modules", "已配置模块数", len(configured), source),
            _row("tplus_export.fallback_modules", "代码默认模块数", len(exports._TPLUS_EXPORT_DESCRIPTIONS), "代码默认"),
        ],
        last_synced_at=_latest_system_config_sync_at("T+导出说明"),
        status="ok" if configured else "warn",
    )


def _inventory_domain() -> dict[str, Any]:
    record = exports._system_config_record("库存仓库范围")
    raw_codes, finished_excluded = exports._inventory_scope_config()
    source = "系统配置镜像" if record else "代码默认"
    return _domain(
        "inventory_warehouse",
        "库存仓库范围",
        "飞书系统配置 / 库存仓库范围",
        _feishu_table_url("FEISHU_SYSTEM_CONFIG_INVENTORY_WAREHOUSE_TABLE_ID"),
        source,
        [
            _row("inventory.raw_warehouses", "原料库存仓库", raw_codes, source),
            _row("inventory.finished_excluded", "成品排除仓库", finished_excluded, source),
        ],
        last_synced_at=_latest_system_config_sync_at("库存仓库范围"),
        status="ok" if record else "warn",
    )


def _feature_count() -> tuple[int, str]:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM features WHERE status <> 'disabled'")
                row = cur.fetchone()
        return int(row[0]), "Admin DB"
    except Exception:
        active_defaults = [item for item in DEFAULT_FEATURES if item.get("status") != "disabled"]
        return len(active_defaults), "代码默认"


def _features_domain() -> dict[str, Any]:
    count, source = _feature_count()
    return _domain(
        "features",
        "首页功能入口",
        "Admin UI / 功能入口管理",
        "/admin/#featuresPanel",
        source,
        [_row("features.active_count", "启用/保留入口数", count, source)],
        status="ok" if source == "Admin DB" else "warn",
        note="已有明确编辑家：admin 后台；不迁飞书。",
    )


@router.get("/v1/admin/system-config/effective")
def effective_system_config(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return {
        "items": [
            _doc_sync_domain(),
            _chat_mode_domain(),
            _tplus_export_domain(),
            _inventory_domain(),
            _features_domain(),
        ]
    }
