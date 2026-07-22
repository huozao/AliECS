"""黄金跨市场价差告警：受鉴权接收，固定投递到配置的飞书群。"""

from __future__ import annotations

import hmac
import os
from contextlib import closing
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from app.core import _conn
from app.routers.versions import send_feishu_text


router = APIRouter()


class GoldSpreadAlert(BaseModel):
    event_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
    kind: Literal[
        "anomaly_started",
        "anomaly_escalated",
        "anomaly_recovered",
        "wrong_price_detected",
        "historical_complete",
        "historical_failed",
    ]
    occurred_at: datetime
    source: Literal["live", "historical"]
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
    summary: str = Field(default="", max_length=2000)

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


def render_gold_spread_alert(alert: GoldSpreadAlert) -> str:
    if alert.kind in {"historical_complete", "historical_failed"}:
        icon = "✅" if alert.kind == "historical_complete" else "⛔"
        title = "历史价差回溯完成" if alert.kind == "historical_complete" else "历史价差回溯失败"
        return f"{icon} {title}\n时间：{alert.occurred_at.isoformat()}\n{alert.summary}".rstrip()

    if alert.kind == "wrong_price_detected":
        contract_name = (alert.contract_name or alert.symbol).replace("【历史回放验证】", "").strip()
        market_names = {
            "SHFE_AU_TRADE": "AU行情快照最新价",
            "SHFE_AU_1S_LOW": "AU 1秒K线最低价",
            "SHFE_AU_1S_HIGH": "AU 1秒K线最高价",
            "SHFE_AU_LOW_UPDATE": "AU交易日最低价更新",
            "SHFE_AU_HIGH_UPDATE": "AU交易日最高价更新",
            "MT5_XAUUSD_TICK": "MT5 XAUUSD 逐笔报价",
        }
        price_labels = {
            "SHFE_AU_1S_LOW": "1秒最低价",
            "SHFE_AU_1S_HIGH": "1秒最高价",
            "SHFE_AU_LOW_UPDATE": "交易日最低价",
            "SHFE_AU_HIGH_UPDATE": "交易日最高价",
        }
        market_name = market_names.get(alert.trigger_market, alert.trigger_market)
        is_au_trigger = alert.trigger_market.startswith("SHFE_AU_")
        trigger_unit = "元/克" if is_au_trigger else "美元/盎司"
        price_label = price_labels.get(alert.trigger_market, "异常价格")
        normal_au_price = (
            alert.international_cny_per_g + alert.baseline_cny_per_g
            if alert.international_cny_per_g is not None and alert.baseline_cny_per_g is not None
            else None
        )
        normal_international = (
            alert.au_mid - alert.baseline_cny_per_g
            if alert.au_mid is not None and alert.baseline_cny_per_g is not None
            else None
        )
        threshold_percent = (
            alert.threshold_cny_per_g / alert.international_cny_per_g * 100
            if alert.threshold_cny_per_g is not None and alert.international_cny_per_g
            else None
        )
        direction = "异常高价" if alert.direction == "up" else "异常低价"
        title = "🧪 历史回放验证" if alert.source == "historical" else "🔴 疑似错价成交预警"
        exchange = "上期所" if alert.exchange_name == "上海期货交易所" else alert.exchange_name
        contract_details = "｜".join(
            item for item in (exchange, alert.contract_month, alert.symbol) if item
        )
        lines = [
            f"{title}｜{contract_name}",
            contract_details,
            f"时间：{_human_time(alert.occurred_at)}",
            "",
            "【异常核心】",
            f"{price_label}：{_number(alert.trigger_price, 2)} {trigger_unit}",
        ]
        if is_au_trigger:
            lines.append(f"正常估值：{_number(normal_au_price, 2)} 元/克")
        else:
            lines.append(f"异常折算：{_number(alert.international_cny_per_g, 2)} 元/克")
            lines.append(f"正常折算：{_number(normal_international, 2)} 元/克")
        lines.extend(
            [
                f"偏离：{_number(alert.deviation_cny_per_g, 2)} 元/克（{_number(alert.deviation_percent, 2)}%）",
                f"触发阈值：{_number(alert.threshold_cny_per_g, 2)} 元/克（{_number(threshold_percent, 2)}%）",
                f"方向：{direction}",
                "",
                "【市场快照】",
                f"AU盘口：{_number(alert.au_bid, 2)} / {_number(alert.au_ask, 2)} 元/克",
                f"XAUUSD：{_number(alert.xau_bid, 2)} / {_number(alert.xau_ask, 2)} 美元/盎司",
                f"USD/CNH：{_number(alert.usdcnh, 5)}",
                f"国际折算：{_number(alert.international_cny_per_g, 2)} 元/克",
                f"近10分钟升贴水：{_number(alert.baseline_cny_per_g, 2)} 元/克",
                "",
                "【数据说明】",
                f"触发来源：{market_name}",
                f"跨市场时差：{_number(alert.clock_skew_ms, 0)} ms",
            ]
        )
        if alert.trigger_market.startswith("SHFE_AU_1S_") and alert.volume_delta is not None:
            lines.append(
                f"该秒成交：{_number(alert.volume_delta, 0)} 手（区间汇总，并非全部成交在异常价）"
            )
        elif alert.trigger_volume is not None:
            lines.append(
                f"快照成交：累计{_number(alert.trigger_volume, 0)}手，区间增加{_number(alert.volume_delta, 0)}手"
            )
        if alert.source == "historical":
            lines.append("⚠️ 这是历史回放，不是当前行情")
        return "\n".join(lines)

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
                    Jsonb(alert.model_dump(mode="json")),
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
    chat_id = os.getenv("GOLD_SPREAD_FEISHU_RECEIVE_ID", "").strip()
    if not chat_id:
        raise HTTPException(status_code=503, detail="gold spread Feishu chat is not configured")
    text = render_gold_spread_alert(body)
    if not _claim_alert(body, text, chat_id):
        return {"ok": True, "sent": False, "duplicate": True, "event_id": body.event_id}
    sent = send_feishu_text(
        chat_id,
        text,
        app_id=os.getenv("VERSION_DIGEST_FEISHU_APP_ID", "").strip(),
        app_secret=os.getenv("VERSION_DIGEST_FEISHU_APP_SECRET", "").strip(),
    )
    if not sent:
        _mark_alert(body.event_id, "failed", "Feishu API returned failure")
        raise HTTPException(status_code=502, detail="Feishu send failed")
    _mark_alert(body.event_id, "sent")
    return {"ok": True, "sent": True, "duplicate": False, "event_id": body.event_id}
