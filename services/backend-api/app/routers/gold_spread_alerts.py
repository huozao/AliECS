"""黄金跨市场价差告警：受鉴权接收，固定投递到配置的飞书群。"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import urllib.request
import uuid
from contextlib import closing
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from app.core import _conn
from app.notify import dispatch
from app.notify.models import Notification


router = APIRouter()


class HistoricalBucket(BaseModel):
    label: str = Field(max_length=24)
    count: int = Field(ge=0)
    share_percent: float = Field(ge=0, le=100)


class HistoricalContractSummary(BaseModel):
    symbol: str = Field(max_length=40)
    total_events: int = Field(ge=0)
    active_days: int = Field(ge=0)
    eligible_seconds: int = Field(ge=0)
    events_per_10000_seconds: float | None = Field(default=None, ge=0)
    active_days_per_event: float | None = Field(default=None, ge=0)
    maximum_deviation_percent: float = Field(ge=0)
    up_count: int = Field(ge=0)
    down_count: int = Field(ge=0)
    buckets: list[HistoricalBucket] = Field(default_factory=list, max_length=100)
    zoom_buckets: list[HistoricalBucket] = Field(default_factory=list, max_length=20)


class AlertChart(BaseModel):
    """任意告警都可以带的配图。历史回溯报告和逐笔错单共用这一个结构。"""

    name: str = Field(max_length=120)
    content_type: Literal["image/png"] = "image/png"
    caption: str = Field(max_length=120)
    data_base64: str = Field(max_length=4_000_000)


class HistoricalMaximumEvent(BaseModel):
    symbol: str = Field(max_length=40)
    occurred_at: datetime
    deviation_percent: float = Field(ge=0)
    direction: Literal["up", "down"]


class HistoricalAnalysis(BaseModel):
    schema_version: Literal[1]
    product_code: str = Field(min_length=1, max_length=20)
    product_name: str = Field(min_length=1, max_length=80)
    period_start: date
    period_end: date
    contract_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    focus_event_count: int = Field(ge=0)
    active_contract_days: int = Field(ge=0)
    eligible_seconds: int = Field(ge=0)
    maximum_deviation_percent: float = Field(ge=0)
    maximum_event: HistoricalMaximumEvent | None = None
    overall_buckets: list[HistoricalBucket] = Field(default_factory=list, max_length=100)
    zoom_buckets: list[HistoricalBucket] = Field(default_factory=list, max_length=20)
    contracts: list[HistoricalContractSummary] = Field(default_factory=list, max_length=100)
    charts: list[AlertChart] = Field(default_factory=list, max_length=4)
    report_files: list[str] = Field(default_factory=list, max_length=20)


class ReplayMetric(BaseModel):
    metric: str = Field(min_length=1, max_length=120)
    count: int = Field(ge=0)
    denominator: int = Field(ge=0)


class ReplayProgress(BaseModel):
    job_id: str = Field(min_length=1, max_length=160)
    status_code: str = Field(min_length=1, max_length=120)
    phase: str = Field(min_length=1, max_length=40)
    completed_partitions: int = Field(ge=0)
    total_partitions: int = Field(gt=0)
    worker_process_count: int = Field(ge=0)
    started_at: datetime | None = None
    status_updated_at: datetime | None = None
    latest_artifact_at: datetime | None = None
    elapsed_seconds: int | None = Field(default=None, ge=0)
    estimated_remaining_seconds: int | None = Field(default=None, ge=0)
    estimate_basis: str = Field(default="", max_length=240)
    result_file_count: int | None = Field(default=None, ge=0)
    error_type: str = Field(default="", max_length=120)
    error_message: str = Field(default="", max_length=1000)
    report_metrics: list[ReplayMetric] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_partition_progress(self) -> "ReplayProgress":
        if self.completed_partitions > self.total_partitions:
            raise ValueError("completed_partitions exceeds total_partitions")
        return self


class GoldSpreadAlert(BaseModel):
    event_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
    kind: Literal[
        "anomaly_started",
        "anomaly_escalated",
        "anomaly_recovered",
        "wrong_price_detected",
        "wrong_price_review",
        "historical_complete",
        "historical_failed",
        "data_silence",
        "data_silence_recovered",
        "replay_summary",
    ]
    occurred_at: datetime
    source: Literal["live", "historical", "replay"]
    severity: Literal["info", "warning", "critical"] = "warning"
    symbol: str = Field(default="", max_length=40)
    contract_name: str = Field(default="", max_length=80)
    exchange_name: str = Field(default="", max_length=80)
    contract_month: str = Field(default="", max_length=20)
    contract_expire_at: datetime | None = None
    direction: Literal["up", "down", ""] = ""
    trigger_market: Literal[
        "SHFE_AU_TRADE",
        "SHFE_AU_1S_LOW",
        "SHFE_AU_1S_HIGH",
        "SHFE_AU_LOW_UPDATE",
        "SHFE_AU_HIGH_UPDATE",
        "MT5_XAUUSD_TICK",
        "",
    ] = ""
    trigger_price: float | None = Field(default=None, gt=0)
    trigger_volume: float | None = Field(default=None, ge=0)
    volume_delta: float | None = Field(default=None, gt=0)
    trigger_tick_id: str = Field(default="", max_length=200)
    au_bid: float | None = Field(default=None, gt=0)
    au_ask: float | None = Field(default=None, gt=0)
    au_mid: float | None = Field(default=None, gt=0)
    xauusd: float | None = Field(default=None, gt=0)
    xau_bid: float | None = Field(default=None, gt=0)
    xau_ask: float | None = Field(default=None, gt=0)
    usdcnh: float | None = Field(default=None, gt=0)
    international_cny_per_g: float | None = Field(default=None, gt=0)
    spread_cny_per_g: float | None = None
    baseline_cny_per_g: float | None = None
    deviation_cny_per_g: float | None = None
    threshold_cny_per_g: float | None = Field(default=None, gt=0)
    peak_deviation_cny_per_g: float | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    deviation_percent: float | None = None
    clock_skew_ms: float | None = Field(default=None, ge=0)
    book_breach_cny_per_g: float | None = Field(default=None, ge=0)
    related_event_id: str = Field(default="", max_length=200)
    related_occurred_at: datetime | None = None
    recovered: bool | None = None
    recovery_seconds: float | None = Field(default=None, ge=0)
    summary: str = Field(default="", max_length=5000)
    # 任意 kind 都可以带图（逐笔错单的四面板明细图走这里）。历史回溯报告的图
    # 在 historical_analysis.charts 里，两者不能同时给，否则不知道该发哪张卡。
    charts: list[AlertChart] = Field(default_factory=list, max_length=4)
    historical_analysis: HistoricalAnalysis | None = None
    replay_progress: ReplayProgress | None = None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "GoldSpreadAlert":
        if self.kind.startswith("anomaly_") or self.kind == "wrong_price_detected":
            required = {
                "symbol": self.symbol,
                "direction": self.direction,
                "au_mid": self.au_mid,
                "xauusd": self.xauusd,
                "usdcnh": self.usdcnh,
                "international_cny_per_g": self.international_cny_per_g,
                "spread_cny_per_g": self.spread_cny_per_g,
                "baseline_cny_per_g": self.baseline_cny_per_g,
                "deviation_cny_per_g": self.deviation_cny_per_g,
                "threshold_cny_per_g": self.threshold_cny_per_g,
            }
            missing = [name for name, value in required.items() if value in (None, "")]
            if missing:
                raise ValueError("anomaly alert missing fields: " + ", ".join(missing))
        if self.kind == "wrong_price_detected":
            required = {
                "trigger_market": self.trigger_market,
                "trigger_price": self.trigger_price,
                "trigger_tick_id": self.trigger_tick_id,
                "deviation_percent": self.deviation_percent,
                "clock_skew_ms": self.clock_skew_ms,
            }
            missing = [name for name, value in required.items() if value in (None, "")]
            if missing:
                raise ValueError("wrong-price alert missing fields: " + ", ".join(missing))
        if self.kind == "wrong_price_review":
            required = {
                "symbol": self.symbol,
                "direction": self.direction,
                "related_event_id": self.related_event_id,
                "deviation_cny_per_g": self.deviation_cny_per_g,
                "threshold_cny_per_g": self.threshold_cny_per_g,
            }
            missing = [name for name, value in required.items() if value in (None, "")]
            if missing:
                raise ValueError("wrong-price review missing fields: " + ", ".join(missing))
            if self.recovered is None:
                raise ValueError("wrong-price review missing fields: recovered")
            if self.recovered and self.recovery_seconds is None:
                raise ValueError("wrong-price review missing fields: recovery_seconds")
        if self.historical_analysis is not None and self.kind != "historical_complete":
            raise ValueError("historical_analysis is only valid for historical_complete")
        if self.charts and self.historical_analysis is not None:
            raise ValueError("charts and historical_analysis cannot be combined")
        if self.replay_progress is not None and self.kind != "replay_summary":
            raise ValueError("replay_progress is only valid for replay_summary")
        return self


def _require_alert_token(x_gold_spread_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("GOLD_SPREAD_ALERT_TOKEN", "").strip()
    supplied = (x_gold_spread_token or "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="invalid gold spread token")


def _number(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _human_time(value: datetime) -> str:
    rendered = value.strftime("%Y-%m-%d %H:%M:%S")
    if value.microsecond:
        rendered += f".{value.microsecond // 1000:03d}"
    return rendered


def _duration_label(seconds: int | None) -> str:
    if seconds is None:
        return "暂无法可靠估算"
    total_minutes = max(0, seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"


def _replay_title(progress: ReplayProgress) -> str:
    if progress.status_code == "SUCCEEDED｜作业成功":
        return "远端正式复盘、审计与报告已完成"
    if progress.status_code == "FAILED｜作业失败":
        return "远端正式复盘失败"
    return "远端正式复盘进行中"


def _replay_field_groups(progress: ReplayProgress) -> list[list[dict[str, str]]]:
    phase_labels = {
        "replay": "复盘运行",
        "audit": "分区审计",
        "global_audit": "全局审计",
        "report": "汇总报告生成",
        "finalizing": "最终结果清单生成",
        "delivery": "结果交付与校验",
        "complete": "作业完成",
    }
    percentage = progress.completed_partitions / progress.total_partitions * 100
    groups: list[list[dict[str, str]]] = [
        [
            {"name": "作业", "value": progress.job_id},
            {"name": "状态", "value": progress.status_code},
            {"name": "当前阶段", "value": phase_labels.get(progress.phase, progress.phase)},
            {
                "name": "复盘进度",
                "value": (
                    f"{progress.completed_partitions} / {progress.total_partitions} "
                    f"个交易日分区（{percentage:.1f}%）"
                ),
            },
            {"name": "并行进程", "value": f"{progress.worker_process_count} 个分区复盘进程"},
        ]
    ]
    timing: list[dict[str, str]] = []
    if progress.started_at is not None:
        timing.append({"name": "开始时间", "value": _human_time(progress.started_at)})
    if progress.status_updated_at is not None:
        timing.append({"name": "状态更新时间", "value": _human_time(progress.status_updated_at)})
    if progress.latest_artifact_at is not None:
        timing.append({"name": "最新产物写入", "value": _human_time(progress.latest_artifact_at)})
    if progress.elapsed_seconds is not None:
        timing.append({"name": "已运行", "value": _duration_label(progress.elapsed_seconds)})
        estimate = _duration_label(progress.estimated_remaining_seconds)
        if progress.estimate_basis:
            estimate += f"（{progress.estimate_basis}）"
        timing.append({"name": "预计剩余", "value": estimate})
    if timing:
        groups.append(timing)
    result_fields = [
        {
            "name": metric.metric.split("｜", 1)[-1],
            "value": f"{metric.count:,} / {metric.denominator:,}",
        }
        for metric in progress.report_metrics
    ]
    if progress.result_file_count is not None:
        result_fields.append(
            {"name": "结果清单", "value": f"{progress.result_file_count:,} 个已校验文件"}
        )
    if progress.error_type or progress.error_message:
        failure_detail = "｜".join(
            item for item in (progress.error_type, progress.error_message) if item
        )
        result_fields.append(
            {
                "name": "失败详情",
                "value": failure_detail[:512],
            }
        )
    if result_fields:
        groups.append(result_fields)
    return groups


def render_gold_spread_alert(alert: GoldSpreadAlert) -> str:
    if alert.kind in {"historical_complete", "historical_failed"}:
        icon = "✅" if alert.kind == "historical_complete" else "⛔"
        title = "历史价差回溯完成" if alert.kind == "historical_complete" else "历史价差回溯失败"
        return f"{icon} {title}\n时间：{alert.occurred_at.isoformat()}\n{alert.summary}".rstrip()

    if alert.kind in {"wrong_price_detected", "wrong_price_review"}:
        return _render_wrong_price(alert)

    if alert.kind == "replay_summary" and alert.replay_progress is not None:
        lines = [f"🧾 {_replay_title(alert.replay_progress)}", f"时间：{_human_time(alert.occurred_at)}"]
        for group in _replay_field_groups(alert.replay_progress):
            lines.extend(f"{field['name']}：{field['value']}" for field in group)
        return "\n".join(lines)

    if alert.kind in {"data_silence", "data_silence_recovered", "replay_summary"}:
        # 这三类只带 summary，没有行情字段：链路健康类通知，不做价差排版。
        headers = {
            "data_silence": "🔴 行情断流｜价差与错单判定失效",
            "data_silence_recovered": "🟢 行情已恢复",
            "replay_summary": "🧾 收盘复盘",
        }
        return f"{headers[alert.kind]}\n时间：{_human_time(alert.occurred_at)}\n{alert.summary}".rstrip()

    titles = {
        "anomaly_started": "价差异常已确认",
        "anomaly_escalated": "价差异常升级",
        "anomaly_recovered": "价差异常已恢复",
    }
    icons = {"info": "ℹ️", "warning": "⚠️", "critical": "🔴"}
    direction = "向上扩大" if alert.direction == "up" else "向下收窄"
    contract_name = alert.contract_name or alert.symbol
    contract_details = "｜".join(
        item for item in (alert.exchange_name, f"{alert.contract_month}合约" if alert.contract_month else "") if item
    )
    lines = [
        f"{icons[alert.severity]} {titles[alert.kind]}｜{contract_name}｜{direction}",
        f"时间：{alert.occurred_at.isoformat()}",
        f"合约：{contract_name}{f'（{contract_details}）' if contract_details else ''}",
        f"行情代码：{alert.symbol}",
        f"AU 买/卖/中：{_number(alert.au_bid)} / {_number(alert.au_ask)} / {_number(alert.au_mid)} 元/克",
        f"XAUUSD：{_number(alert.xauusd)} 美元/盎司",
        f"USD/CNH：{_number(alert.usdcnh, 5)}",
        f"国际折算：{_number(alert.international_cny_per_g)} 元/克",
        f"当前价差：{_number(alert.spread_cny_per_g)} 元/克",
        f"滚动基线：{_number(alert.baseline_cny_per_g)} 元/克",
        f"偏离/阈值：{_number(alert.deviation_cny_per_g)} / {_number(alert.threshold_cny_per_g)} 元/克",
    ]
    if alert.contract_expire_at is not None:
        lines.insert(4, f"到期时间：{alert.contract_expire_at.isoformat()}")
    if alert.peak_deviation_cny_per_g is not None:
        lines.append(f"最大偏离：{_number(alert.peak_deviation_cny_per_g)} 元/克")
    if alert.duration_seconds is not None:
        lines.append(f"持续时间：{alert.duration_seconds:.1f} 秒")
    if alert.summary:
        lines.append(alert.summary)
    lines.append(f"事件编号：{alert.event_id}")
    return "\n".join(lines)


def _baseline_with_direction(baseline: float | None) -> str:
    if baseline is None:
        return "-"
    side = "内盘升水" if baseline >= 0 else "内盘贴水"
    return f"{baseline:+.2f} 元/克（{side}）"


def _render_wrong_price(alert: GoldSpreadAlert) -> str:
    contract_name = (alert.contract_name or alert.symbol).replace("【历史回放验证】", "").strip()
    exchange = "上期所" if alert.exchange_name == "上海期货交易所" else alert.exchange_name
    contract_details = "｜".join(
        item for item in (exchange, alert.contract_month, alert.symbol) if item
    )
    direction = "异常高价" if alert.direction == "up" else "异常低价"

    if alert.kind == "wrong_price_review":
        occurred = (
            _human_time(alert.related_occurred_at)
            if alert.related_occurred_at is not None
            else "-"
        )
        half_threshold = (
            alert.threshold_cny_per_g * 0.5 if alert.threshold_cny_per_g is not None else None
        )
        if alert.recovered:
            lines = [
                f"🟢 错单复盘｜{contract_name}｜判定成立",
                contract_details,
                f"原事件：{occurred}（{direction}，偏离 {_number(alert.deviation_cny_per_g, 2)} 元/克，"
                f"{_number(alert.deviation_percent, 2)}%）",
                f"价格回归：{_number(alert.recovery_seconds, 1)} 秒回归价差中枢"
                f"（±{_number(half_threshold, 2)} 元内）",
                "三条件齐备：有效成交 ✓ 扫穿盘口 ✓ 快速回归 ✓",
            ]
        else:
            lines = [
                f"⚪ 错单复盘｜{contract_name}｜降级",
                contract_details,
                f"原事件：{occurred}（{direction}，偏离 {_number(alert.deviation_cny_per_g, 2)} 元/克，"
                f"{_number(alert.deviation_percent, 2)}%）",
                "回归窗口内未回归价差中枢，更接近行情重定价，不计为错单",
            ]
        return "\n".join(lines)

    market_names = {
        "SHFE_AU_TRADE": "AU行情快照最新价",
        "SHFE_AU_1S_LOW": "AU 1秒K线最低价",
        "SHFE_AU_1S_HIGH": "AU 1秒K线最高价",
        "SHFE_AU_LOW_UPDATE": "AU交易日最低价更新",
        "SHFE_AU_HIGH_UPDATE": "AU交易日最高价更新",
        "MT5_XAUUSD_TICK": "MT5 XAUUSD 逐笔报价",
    }
    price_labels = {
        "SHFE_AU_TRADE": "成交价",
        "SHFE_AU_1S_LOW": "该秒最低价",
        "SHFE_AU_1S_HIGH": "该秒最高价",
        "SHFE_AU_LOW_UPDATE": "交易日最低价",
        "SHFE_AU_HIGH_UPDATE": "交易日最高价",
    }
    market_name = market_names.get(alert.trigger_market, alert.trigger_market)
    is_au_trigger = alert.trigger_market.startswith("SHFE_AU_")
    trigger_unit = "元/克" if is_au_trigger else "美元/盎司"
    price_label = price_labels.get(alert.trigger_market, "触发价格")
    benchmark_price = (
        alert.international_cny_per_g + alert.baseline_cny_per_g
        if alert.international_cny_per_g is not None and alert.baseline_cny_per_g is not None
        else None
    )
    threshold_percent = (
        alert.threshold_cny_per_g / alert.international_cny_per_g * 100
        if alert.threshold_cny_per_g is not None and alert.international_cny_per_g
        else None
    )
    title = "🧪 历史回放验证" if alert.source == "historical" else "🔴 疑似错单成交"
    lines = [
        f"{title}｜{contract_name}",
        contract_details,
        f"时间：{_human_time(alert.occurred_at)}",
        "",
        "【偏离度】",
        f"极值成交价：{_number(alert.trigger_price, 2)} {trigger_unit}（{price_label}）",
    ]
    if is_au_trigger:
        lines.append(
            f"基准价格：{_number(benchmark_price, 2)} 元/克 = 国际折算 "
            f"{_number(alert.international_cny_per_g, 2)} + 价差中枢 {_number(alert.baseline_cny_per_g, 2)}"
        )
    else:
        # MT5 报价 tick 触发时，异常在国际侧：基准折算不能用异常 tick 自身，
        # 应由 AU 中间价减去价差中枢反推。
        normal_international = (
            alert.au_mid - alert.baseline_cny_per_g
            if alert.au_mid is not None and alert.baseline_cny_per_g is not None
            else None
        )
        lines.append(f"异常折算：{_number(alert.international_cny_per_g, 2)} 元/克")
        lines.append(
            f"基准折算：{_number(normal_international, 2)} 元/克 = AU中间价 "
            f"{_number(alert.au_mid, 2)} − 价差中枢 {_number(alert.baseline_cny_per_g, 2)}"
        )
    lines.extend(
        [
            f"偏离：{_number(alert.deviation_cny_per_g, 2)} 元/克（{_number(alert.deviation_percent, 2)}%）"
            f"｜阈值 {_number(alert.threshold_cny_per_g, 2)} 元/克（{_number(threshold_percent, 2)}%）",
            f"方向：{direction}",
            "",
            "【判定依据】",
        ]
    )
    if alert.trigger_market.startswith("SHFE_AU_1S_") and alert.volume_delta is not None:
        lines.append(f"有效成交：该秒成交 {_number(alert.volume_delta, 0)} 手")
    elif alert.volume_delta is not None:
        lines.append(
            f"有效成交：快照区间成交 {_number(alert.volume_delta, 0)} 手"
            f"（累计 {_number(alert.trigger_volume, 0)} 手）"
        )
    else:
        lines.append("有效成交：无成交记录")
    if alert.book_breach_cny_per_g is None:
        lines.append("扫穿盘口：事发时盘口缺失，无法判定")
    elif alert.book_breach_cny_per_g > 0:
        lines.append(f"扫穿盘口：越出事发时盘口 {_number(alert.book_breach_cny_per_g, 2)} 元")
    else:
        lines.append("扫穿盘口：未越出盘口")
    if alert.source == "historical":
        lines.append("价格回归：回归用时见回溯报告 recovery_seconds 列")
    else:
        lines.append("价格回归：等待复盘（回归窗口结束后追发）")
    lines.extend(
        [
            "",
            "【复盘要素】",
            f"事发时盘口：{_number(alert.au_bid, 2)} / {_number(alert.au_ask, 2)} 元/克",
            f"XAUUSD：{_number(alert.xau_bid, 2)} / {_number(alert.xau_ask, 2)} 美元/盎司",
            f"USD/CNH：{_number(alert.usdcnh, 5)}",
            f"国际折算：{_number(alert.international_cny_per_g, 2)} 元/克",
            f"价差中枢：{_baseline_with_direction(alert.baseline_cny_per_g)}",
            f"触发来源：{market_name}｜跨市场时差：{_number(alert.clock_skew_ms, 0)} ms",
        ]
    )
    if alert.source == "historical":
        lines.append("⚠️ 这是历史回放，不是当前行情")
    return "\n".join(lines)


def _severity_level(severity: str) -> str:
    """业务 severity → 中枢 level。中枢只认 info/warn/error/fatal。"""
    return {"info": "info", "warning": "warn", "critical": "error"}.get(severity, "warn")


def build_alert_notification(alert: GoldSpreadAlert, text: str) -> Notification:
    """把业务告警转成中枢消息。

    这里只负责「说清楚发生了什么」，渲染成飞书卡片或企微 markdown 是 channel 的事。
    收敛前这个文件自己拼卡片、自己传图、自己调 im/v1/messages，四处各写一套；
    现在它只产 segments。
    """
    if alert.historical_analysis is not None:
        return _build_historical_notification(alert)
    if alert.replay_progress is not None:
        segments = [
            {"kind": "fields", "fields": fields}
            for fields in _replay_field_groups(alert.replay_progress)
        ]
        segments.append({"kind": "text", "text": f"事件编号：{alert.event_id}"})
        return Notification.model_validate(
            {
                "source": "gold-spread-monitor",
                "event": alert.kind,
                "level": _severity_level(alert.severity),
                "title": _replay_title(alert.replay_progress),
                "segments": segments,
                "dedup_key": f"gold:{alert.event_id}",
                "occurred_at": alert.occurred_at,
            }
        )

    lines = text.split("\n")
    title = lines[0].strip() or "黄金价差告警"
    body = "\n".join(lines[1:]).strip()

    segments: list[dict[str, Any]] = []
    if body:
        # ⚠️ 必须 preformatted。价差排版里有 *、｜、# 这类字符，交给 markdown 会被吃掉
        # 或变成标题——收敛前的 build_alert_card 用 plain_text 而非 lark_md 正是为此。
        segments.append({"kind": "text", "text": body, "preformatted": True})

    images: list[dict[str, Any]] = []
    for index, chart in enumerate(alert.charts or []):
        ref = f"chart{index}"
        images.append({"ref": ref, "caption": chart.caption, "png_base64": chart.data_base64})
        if chart.caption:
            segments.append({"kind": "text", "text": f"**{chart.caption}**"})
        segments.append({"kind": "image", "image_ref": ref})

    segments.append({"kind": "text", "text": f"事件编号：{alert.event_id}"})
    return Notification.model_validate(
        {
            "source": "gold-spread-monitor",
            "event": alert.kind,
            "level": _severity_level(alert.severity),
            "title": title,
            "segments": segments,
            "images": images,
            "dedup_key": f"gold:{alert.event_id}",
            "occurred_at": alert.occurred_at,
        }
    )


def _build_historical_notification(alert: GoldSpreadAlert) -> Notification:
    """历史回溯报告：文字与图交错的长消息，段落顺序与收敛前的卡片一致。"""
    report = alert.historical_analysis
    if report is None:
        raise ValueError("historical analysis is required")

    segments: list[dict[str, Any]] = [
        {
            "kind": "text",
            "text": (
                f"**扫描范围**\n{report.period_start} 至 {report.period_end}\n\n"
                f"**核心指标**\n合约 **{report.contract_count}** 个　｜　"
                f"疑似事件 **{report.event_count}** 个　｜　"
                f"偏离 ≥1% **{report.focus_event_count}** 个\n"
                f"有效合约交易日 **{report.active_contract_days}**　｜　"
                f"可判定一秒区间 **{report.eligible_seconds}**"
            ),
        }
    ]

    nonzero_buckets = [bucket for bucket in report.overall_buckets if bucket.count]
    distribution = "\n".join(
        f"{bucket.label}：**{bucket.count}**（{bucket.share_percent:.2f}%）"
        for bucket in nonzero_buckets
    )
    segments.append({"kind": "text", "text": f"**总体分布**\n{distribution or '无疑似事件'}"})

    images: list[dict[str, Any]] = []
    for index, chart in enumerate(report.charts or []):
        ref = f"hist{index}"
        images.append({"ref": ref, "caption": chart.caption, "png_base64": chart.data_base64})
        if chart.caption:
            segments.append({"kind": "text", "text": f"**{chart.caption}**"})
        segments.append({"kind": "image", "image_ref": ref})

    contract_lines: list[str] = []
    for item in report.contracts:
        interval = (
            "无事件"
            if item.active_days_per_event is None
            else f"{item.active_days_per_event:.2f}有效日/次"
        )
        rate = (
            "-"
            if item.events_per_10000_seconds is None
            else f"{item.events_per_10000_seconds:.2f}/万秒"
        )
        contract_lines.append(f"{item.symbol}：**{item.total_events}**｜{rate}｜{interval}")
    segments.append(
        {"kind": "text", "text": "**逐合约摘要**\n" + ("\n".join(contract_lines) or "无合约数据")}
    )

    if report.maximum_event is not None:
        maximum = report.maximum_event
        direction = "异常高价" if maximum.direction == "up" else "异常低价"
        segments.append(
            {
                "kind": "text",
                "text": (
                    f"**最大偏离事件**\n{maximum.symbol}｜"
                    f"{maximum.deviation_percent:.2f}%｜{direction}\n"
                    f"{maximum.occurred_at.strftime('%Y-%m-%d %H:%M:%S')}"
                ),
            }
        )

    files = "、".join(report.report_files)
    segments.append(
        {
            "kind": "text",
            "text": (
                "统计单位为经5秒去重后的疑似事件，并非逐笔成交笔数。"
                + (f" 本地报告：{files}" if files else "")
            ),
        }
    )

    return Notification.model_validate(
        {
            "source": "gold-spread-monitor",
            "event": alert.kind,
            "level": "warn" if report.focus_event_count else "info",
            "title": f"{report.product_name}｜历史错价回溯完成",
            "segments": segments,
            "images": images,
            "dedup_key": f"gold:{alert.event_id}",
            "occurred_at": alert.occurred_at,
        }
    )


def deliver_alert(text: str, alert: GoldSpreadAlert) -> dict[str, Any]:
    """交给统一消息中枢，返回发件箱编号和各投递状态计数。

    ⚠️ 收件人现在由 notify_routes 决定，不再读 GOLD_SPREAD_FEISHU_RECEIVE_ID。
    没有配路由时消息会安全落库但没人收到——中枢返回 targets=0，这里视为失败，
    以免「配置缺失」被当成「发送成功」。
    """
    return dispatch.deliver(build_alert_notification(alert, text))


def _strip_chart_bytes(charts: Any) -> None:
    """base64 图不入库——一张几十万字符，payload_json 会被撑爆。只留长度做审计。"""
    for chart in charts or []:
        if isinstance(chart, dict):
            encoded = str(chart.pop("data_base64", ""))
            chart["base64_characters"] = len(encoded)


def _stored_alert_payload(alert: GoldSpreadAlert) -> dict[str, Any]:
    payload = alert.model_dump(mode="json")
    analysis = payload.get("historical_analysis")
    if isinstance(analysis, dict):
        _strip_chart_bytes(analysis.get("charts"))
    _strip_chart_bytes(payload.get("charts"))
    return payload


def _claim_alert(alert: GoldSpreadAlert, rendered_text: str, chat_id: str) -> bool:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM gold_spread_alerts WHERE event_id=%s",
                (alert.event_id,),
            )
            row = cur.fetchone()
            if row and row[0] == "sent":
                return False
            cur.execute(
                """
                INSERT INTO gold_spread_alerts(
                    event_id, kind, occurred_at, source, symbol, severity,
                    chat_id, payload_json, rendered_text, status, attempts
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 1)
                ON CONFLICT(event_id) DO UPDATE SET
                    payload_json=EXCLUDED.payload_json,
                    rendered_text=EXCLUDED.rendered_text,
                    status='pending',
                    attempts=gold_spread_alerts.attempts + 1,
                    last_error='',
                    updated_at=NOW()
                """,
                (
                    alert.event_id,
                    alert.kind,
                    alert.occurred_at,
                    alert.source,
                    alert.symbol,
                    alert.severity,
                    chat_id,
                    Jsonb(_stored_alert_payload(alert)),
                    rendered_text,
                ),
            )
        conn.commit()
    return True


def _mark_alert(event_id: str, status: str, error_text: str = "") -> None:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gold_spread_alerts
                SET status=%s, last_error=%s,
                    sent_at=CASE WHEN %s='sent' THEN NOW() ELSE sent_at END,
                    updated_at=NOW()
                WHERE event_id=%s
                """,
                (status, error_text[:1000], status, event_id),
            )
        conn.commit()


@router.post("/v1/internal/gold-spread/alerts")
def send_gold_spread_alert(
    body: GoldSpreadAlert,
    _: None = Depends(_require_alert_token),
) -> dict[str, Any]:
    text = render_gold_spread_alert(body)
    # chat_id 只留作业务表的历史字段：投递目标现在由 notify_routes 决定，
    # 这个 env 不再影响发到哪里（留空也能发）。
    chat_id = os.getenv("GOLD_SPREAD_FEISHU_RECEIVE_ID", "").strip()
    if not _claim_alert(body, text, chat_id):
        # _claim_alert 只在业务表已有 sent 状态时返回 False，因此这是已送达事件的幂等重试。
        return {
            "ok": True,
            "delivered": True,
            "sent": 1,
            "duplicate": True,
            "event_id": body.event_id,
        }
    delivery = deliver_alert(text, body)
    # 兼容测试替身和滚动升级期间的旧布尔返回；生产实现返回统一投递结果字典。
    delivery_ok = delivery if isinstance(delivery, bool) else bool(delivery.get("sent"))
    if not delivery_ok:
        _mark_alert(body.event_id, "failed", "notify center delivered to no target")
        raise HTTPException(status_code=502, detail="notify delivery failed")
    _mark_alert(body.event_id, "sent")
    receipt = (
        {"sent": 1, "duplicate": False}
        if isinstance(delivery, bool)
        else delivery
    )
    return {"ok": True, "delivered": True, "event_id": body.event_id, **receipt}
