from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.providers.feishu import FeishuBitableClient, credentials_for_profile, env_profiles
from app.storage.postgres import normalize_record, open_store


BEIJING = timezone(timedelta(hours=8))
CONFIG_TABLE_SHEET_NAME = "配置表"
CONFIG_PROVIDER = "doc_sync"
_ANCHOR_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TRUTHY = {"1", "true", "yes", "y", "on", "是", "开", "启用", "√", "✓", "checked"}
_FALSY = {"0", "false", "no", "n", "off", "否", "关", "停用", "禁用", ""}

# 配置项注册表：配置键 → (DB 字段, 解析函数)。解析失败抛 ValueError。
INTERVAL_MIN_HOURS = 1.0
INTERVAL_MAX_HOURS = 168.0


def _parse_enabled(raw: str) -> bool:
    text = raw.strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    raise ValueError(f"无法识别的开关值：{raw}")


def _parse_interval_seconds(raw: str) -> int:
    hours = float(raw.strip())
    if not (INTERVAL_MIN_HOURS <= hours <= INTERVAL_MAX_HOURS):
        raise ValueError(f"周期小时超出范围（{INTERVAL_MIN_HOURS}-{INTERVAL_MAX_HOURS}）：{raw}")
    return int(round(hours * 3600))


def _parse_anchor(raw: str) -> str:
    text = raw.strip()
    if text and not _ANCHOR_RE.match(text):
        raise ValueError(f"起点时间须为 HH:MM（北京时间）：{raw}")
    return text


CONFIG_REGISTRY = {
    "文档同步开关": ("enabled", _parse_enabled),
    "文档同步周期小时": ("interval_seconds", _parse_interval_seconds),
    "文档同步起点时间": ("anchor_time", _parse_anchor),
}


def _cell_text(value: Any) -> str:
    """Bitable 单元格转纯文本：字符串直用；富文本段落列表拼 text；布尔转 true/false。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value if value is not None else "")


def parse_config_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """按注册表解析「配置表」行：状态列非空且≠启用的行跳过；未知键忽略；非法值进错误列表。"""
    config: dict[str, Any] = {}
    errors: list[str] = []
    for row in rows:
        key = _cell_text(row.get("配置键")).strip()
        if key not in CONFIG_REGISTRY:
            continue
        status = _cell_text(row.get("状态")).strip()
        if status and status != "启用":
            continue
        field, parser = CONFIG_REGISTRY[key]
        raw = _cell_text(row.get("配置值"))
        try:
            config[field] = parser(raw)
        except (ValueError, TypeError) as exc:
            errors.append(f"{key}: {exc}")
    return config, errors


def next_full_sync_due(
    now: datetime, last_full: datetime | None, interval_seconds: int, anchor_time: str
) -> datetime:
    """下一次全量同步应跑的时刻（aware-UTC）。从未跑过=立即；无锚点=上次+周期；
    锚点 HH:MM（北京时间）=相位对齐到 {锚点 + k*周期} 序列中大于上次的最小值。"""
    interval = max(int(interval_seconds), 60)
    if last_full is None:
        return now
    if not anchor_time:
        return last_full + timedelta(seconds=interval)
    hour, minute = (int(part) for part in anchor_time.split(":"))
    anchor_local = last_full.astimezone(BEIJING).replace(hour=hour, minute=minute, second=0, microsecond=0)
    anchor = anchor_local.astimezone(timezone.utc)
    # 把 anchor 移到 last_full 之前，再逐周期前进到第一个大于 last_full 的点。
    if anchor > last_full:
        steps = int((anchor - last_full).total_seconds() // interval) + 1
        anchor -= timedelta(seconds=steps * interval)
    due = anchor + timedelta(seconds=interval)
    while due <= last_full:
        due += timedelta(seconds=interval)
    return due


def _env_interval_seconds() -> int:
    try:
        value = int(str(os.getenv("DOC_SYNC_INTERVAL_SECONDS", "")).strip() or 86400)
    except ValueError:
        return 86400
    return value if value > 0 else 86400


def default_schedule_config() -> dict[str, Any]:
    return {"enabled": True, "interval_seconds": _env_interval_seconds(), "anchor_time": "", "pull_paused": False}


def read_schedule_config() -> dict[str, Any]:
    """DB 热读 doc_sync 调度配置；DB 不可用/无行一律回退 env 默认（不抛）。"""
    try:
        store = open_store()
        try:
            row = store.get_sync_config(CONFIG_PROVIDER)
        finally:
            store.close()
        if row:
            return {
                "enabled": bool(row["enabled"]),
                "interval_seconds": int(row["interval_seconds"]),
                "anchor_time": str(row["anchor_time"] or ""),
                "pull_paused": bool(row["pull_paused"]),
            }
    except Exception:  # noqa: BLE001 - 配置读取失败不拖垮 worker
        pass
    return default_schedule_config()


def read_last_full_run() -> datetime | None:
    try:
        store = open_store()
        try:
            return store.last_full_run_started_at()
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        return None


def pull_config_from_bitable() -> str:
    """从飞书「配置表」单向拉取调度配置落 DB（表格=编辑面，DB=生效面）。
    返回状态字符串（记日志用），任何异常吞掉返回错误串。"""
    try:
        store = open_store()
    except Exception as exc:  # noqa: BLE001
        return f"skipped: store unavailable ({exc})"
    try:
        current = store.get_sync_config(CONFIG_PROVIDER)
        if not current:
            return "skipped: doc_sync config row missing"
        if current.get("pull_paused"):
            return "paused: 管理页已暂停表格拉取"
        for profile in env_profiles(""):
            sources = [
                row
                for row in store.list_bitable_sources("feishu", profile)
                if str(row.get("sheet_name") or "") == CONFIG_TABLE_SHEET_NAME
            ]
            if not sources:
                continue
            credentials = credentials_for_profile(profile)
            if not credentials:
                continue
            credential = credentials[0]
            client = FeishuBitableClient(
                app_id=credential.app_id,
                app_secret=credential.app_secret,
                api_base=credential.api_base,
            )
            source = sources[0]
            app_token = str(source.get("external_doc_id") or "")
            table_id = str(source.get("external_sheet_id") or "")
            fields = client.list_fields(app_token, table_id)
            field_titles = {
                str(field.get("field_id")): str(field.get("field_title") or field.get("field_name") or "")
                for field in fields
            }
            records = client.get_records(app_token, table_id).get("records") or []
            rows = [normalize_record(record, field_titles) for record in records if isinstance(record, dict)]
            config, errors = parse_config_rows(rows)
            for error in errors:
                print(f"[配置表拉取] 非法值已跳过：{error}")
            if not config:
                return "noop: 配置表无可用配置项"
            merged = {
                "enabled": config.get("enabled", current["enabled"]),
                "interval_seconds": config.get("interval_seconds", current["interval_seconds"]),
                "anchor_time": config.get("anchor_time", current["anchor_time"]),
            }
            unchanged = all(merged[key] == current[key] for key in merged)
            if unchanged:
                return "noop: 配置无变化"
            store.upsert_sync_config(
                CONFIG_PROVIDER,
                enabled=bool(merged["enabled"]),
                interval_seconds=int(merged["interval_seconds"]),
                anchor_time=str(merged["anchor_time"]),
                updated_by="feishu-config-table",
            )
            return f"applied: {merged}"
        return "noop: 未找到「配置表」数据源"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"
    finally:
        store.close()
