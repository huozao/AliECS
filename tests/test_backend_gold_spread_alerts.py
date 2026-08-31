from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))

from app.routers import gold_spread_alerts as alerts  # noqa: E402


def _payload(kind: str = "anomaly_started") -> dict[str, object]:
    return {
        "event_id": "SHFE.au2606:20260519T210435500:start",
        "kind": kind,
        "occurred_at": "2026-05-19T21:04:35.500+08:00",
        "source": "live",
        "severity": "warning",
        "symbol": "SHFE.au2606",
        "contract_name": "沪金 AU2606",
        "exchange_name": "上海期货交易所",
        "contract_month": "2026年06月",
        "contract_expire_at": "2026-06-15T15:00:00+08:00",
        "direction": "down",
        "au_bid": 994.84,
        "au_ask": 995.28,
        "au_mid": 995.06,
        "xauusd": 4526.535,
        "usdcnh": 6.8102,
        "international_cny_per_g": 991.098483,
        "spread_cny_per_g": 3.961517,
        "baseline_cny_per_g": 4.387448,
        "deviation_cny_per_g": -0.425931,
        "threshold_cny_per_g": 0.3,
    }


def _client(monkeypatch, *, sent: bool = True, claimed: bool = True) -> TestClient:
    monkeypatch.setenv("GOLD_SPREAD_ALERT_TOKEN", "test-token")
    monkeypatch.setenv("GOLD_SPREAD_FEISHU_RECEIVE_ID", "oc_target")
    monkeypatch.setenv("VERSION_DIGEST_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("VERSION_DIGEST_FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setattr(alerts, "_claim_alert", lambda *_: claimed)
    monkeypatch.setattr(alerts, "_mark_alert", lambda *_: None)
    monkeypatch.setattr(alerts, "deliver_alert", lambda *_, **__: sent)
    app = FastAPI()
    app.include_router(alerts.router)
    return TestClient(app)


def test_alert_rejects_wrong_token(monkeypatch) -> None:
    response = _client(monkeypatch).post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "wrong"},
        json=_payload(),
    )
    assert response.status_code == 401


def test_alert_sends_to_fixed_chat(monkeypatch) -> None:
    captured: dict[str, object] = {}
    client = _client(monkeypatch)

    def fake_send(text: str, alert=None) -> bool:
        captured.update(text=text, alert=alert)
        return True

    monkeypatch.setattr(alerts, "deliver_alert", fake_send)
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=_payload(),
    )
    assert response.status_code == 200
    assert "合约：沪金 AU2606（上海期货交易所｜2026年06月合约）" in str(captured["text"])
    assert "到期时间：2026-06-15T15:00:00+08:00" in str(captured["text"])
    assert "行情代码：SHFE.au2606" in str(captured["text"])
    assert "AU 买/卖/中：994.840 / 995.280 / 995.060" in str(captured["text"])
    assert "XAUUSD：4526.535" in str(captured["text"])
    assert "偏离/阈值：-0.426 / 0.300" in str(captured["text"])


def test_duplicate_alert_is_not_sent_again(monkeypatch) -> None:
    response = _client(monkeypatch, claimed=False).post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=_payload(),
    )
    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert response.json()["delivered"] is True
    assert response.json()["sent"] == 1


def test_history_alert_does_not_require_market_prices(monkeypatch) -> None:
    body = {
        "event_id": "history:20260401:20260722:complete",
        "kind": "historical_complete",
        "occurred_at": "2026-07-22T13:30:00+08:00",
        "source": "historical",
        "severity": "info",
        "summary": "区间 2026-04-01 至 2026-07-22，确认异常 12 个。",
    }
    response = _client(monkeypatch).post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 200


def test_wrong_price_alert_contains_trade_evidence(monkeypatch) -> None:
    body = _payload()
    body.update(
        event_id="wrong:au:SHFE.au2606:1789765475500000000:12345",
        kind="wrong_price_detected",
        severity="critical",
        trigger_market="SHFE_AU_TRADE",
        trigger_price=982.0,
        trigger_volume=8012,
        volume_delta=1,
        trigger_tick_id="1789765475500000000:12345",
        au_mid=982.0,
        spread_cny_per_g=-9.098483,
        deviation_cny_per_g=-13.485931,
        threshold_cny_per_g=1.0,
        deviation_percent=-1.3607,
        clock_skew_ms=125.0,
        book_breach_cny_per_g=12.84,
    )
    captured: dict[str, object] = {}
    client = _client(monkeypatch)

    def fake_send(text: str, alert=None) -> bool:
        captured.update(text=text, alert=alert)
        return True

    monkeypatch.setattr(alerts, "deliver_alert", fake_send)
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 200
    text = str(captured["text"])
    assert "🔴 疑似错单成交｜沪金 AU2606" in text
    assert "上期所｜2026年06月｜SHFE.au2606" in text
    assert "时间：2026-05-19 21:04:35.500" in text
    assert "【偏离度】" in text
    assert "极值成交价：982.00 元/克（成交价）" in text
    assert "基准价格：995.49 元/克 = 国际折算 991.10 + 价差中枢 4.39" in text
    assert "偏离：-13.49 元/克（-1.36%）｜阈值 1.00 元/克（0.10%）" in text
    assert "【判定依据】" in text
    assert "有效成交：快照区间成交 1 手（累计 8012 手）" in text
    assert "扫穿盘口：越出事发时盘口 12.84 元" in text
    assert "价格回归：等待复盘（回归窗口结束后追发）" in text
    assert "【复盘要素】" in text
    assert "价差中枢：+4.39 元/克（内盘升水）" in text
    assert "触发来源：AU行情快照最新价｜跨市场时差：125 ms" in text
    assert "Tick ID" not in text
    assert "事件编号" not in text


def test_wrong_price_alert_explains_one_second_low_volume(monkeypatch) -> None:
    body = _payload()
    body.update(
        event_id="wrong:history:SHFE.au2606:1779195875000:8006011:SHFE_AU_1S_LOW",
        kind="wrong_price_detected",
        source="historical",
        severity="critical",
        trigger_market="SHFE_AU_1S_LOW",
        trigger_price=830.52,
        trigger_volume=None,
        volume_delta=370,
        trigger_tick_id="1779195875000:8006011:SHFE_AU_1S_LOW",
        au_mid=830.52,
        xau_bid=4526.365,
        xau_ask=4527.045,
        xauusd=4526.705,
        international_cny_per_g=991.135705,
        spread_cny_per_g=-160.615705,
        baseline_cny_per_g=4.38709,
        deviation_cny_per_g=-165.002795,
        threshold_cny_per_g=1.0,
        deviation_percent=-16.647851,
        clock_skew_ms=1213.0,
        book_breach_cny_per_g=165.08,
    )
    captured: dict[str, object] = {}
    client = _client(monkeypatch)

    def fake_send(text: str, alert=None) -> bool:
        captured.update(text=text, alert=alert)
        return True

    monkeypatch.setattr(alerts, "deliver_alert", fake_send)
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 200
    text = str(captured["text"])
    assert "🧪 历史回放验证｜沪金 AU2606" in text
    assert "极值成交价：830.52 元/克（该秒最低价）" in text
    assert "基准价格：995.52 元/克 = 国际折算 991.14 + 价差中枢 4.39" in text
    assert "偏离：-165.00 元/克（-16.65%）" in text
    assert "有效成交：该秒成交 370 手" in text
    assert "扫穿盘口：越出事发时盘口 165.08 元" in text
    assert "价格回归：回归用时见回溯报告 recovery_seconds 列" in text
    assert "⚠️ 这是历史回放，不是当前行情" in text
    assert "【历史回放验证】" not in text


def test_wrong_price_mt5_trigger_uses_au_side_benchmark(monkeypatch) -> None:
    body = _payload()
    body.update(
        event_id="wrong:xau:SHFE.au2702:1784241602385",
        kind="wrong_price_detected",
        severity="critical",
        direction="up",
        trigger_market="MT5_XAUUSD_TICK",
        trigger_price=4048.45,
        trigger_tick_id="1784241602385:4048.23:4048.68",
        au_bid=891.84,
        au_ask=892.88,
        au_mid=892.36,
        xauusd=4048.45,
        usdcnh=6.77581,
        international_cny_per_g=881.94,
        spread_cny_per_g=10.42,
        baseline_cny_per_g=9.39,
        deviation_cny_per_g=1.03,
        threshold_cny_per_g=1.0,
        deviation_percent=0.12,
        clock_skew_ms=885.0,
    )
    captured: dict[str, object] = {}
    client = _client(monkeypatch)

    def fake_send(text: str, alert=None) -> bool:
        captured.update(text=text, alert=alert)
        return True

    monkeypatch.setattr(alerts, "deliver_alert", fake_send)
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 200
    text = str(captured["text"])
    assert "极值成交价：4048.45 美元/盎司（触发价格）" in text
    assert "异常折算：881.94 元/克" in text
    # 基准折算须由 AU 中间价反推（892.36 − 9.39），不能用异常 tick 自身折算
    assert "基准折算：882.97 元/克 = AU中间价 892.36 − 价差中枢 9.39" in text
    assert "有效成交：无成交记录" in text


def test_wrong_price_review_recovered_confirms_fat_finger(monkeypatch) -> None:
    body = _payload()
    body.update(
        event_id="wrong:au:SHFE.au2606:1779195875000:8006011:review",
        kind="wrong_price_review",
        severity="warning",
        direction="down",
        related_event_id="wrong:au:SHFE.au2606:1779195875000:8006011",
        related_occurred_at="2026-05-19T21:04:35+08:00",
        recovered=True,
        recovery_seconds=0.5,
        trigger_price=830.52,
        volume_delta=370,
        book_breach_cny_per_g=165.08,
        deviation_cny_per_g=-165.002795,
        threshold_cny_per_g=1.0,
        deviation_percent=-16.647851,
    )
    captured: dict[str, object] = {}
    client = _client(monkeypatch)

    def fake_send(text: str, alert=None) -> bool:
        captured.update(text=text, alert=alert)
        return True

    monkeypatch.setattr(alerts, "deliver_alert", fake_send)
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 200
    text = str(captured["text"])
    assert "🟢 错单复盘｜沪金 AU2606｜判定成立" in text
    assert "原事件：2026-05-19 21:04:35（异常低价，偏离 -165.00 元/克，-16.65%）" in text
    assert "价格回归：0.5 秒回归价差中枢（±0.50 元内）" in text
    assert "三条件齐备：有效成交 ✓ 扫穿盘口 ✓ 快速回归 ✓" in text


def test_wrong_price_review_timeout_downgrades(monkeypatch) -> None:
    body = _payload()
    body.update(
        event_id="wrong:au:SHFE.au2606:1779195875000:8006012:review",
        kind="wrong_price_review",
        severity="info",
        direction="up",
        related_event_id="wrong:au:SHFE.au2606:1779195875000:8006012",
        related_occurred_at="2026-05-19T21:04:35+08:00",
        recovered=False,
        deviation_cny_per_g=1.2,
        threshold_cny_per_g=1.0,
        deviation_percent=0.12,
    )
    captured: dict[str, object] = {}
    client = _client(monkeypatch)

    def fake_send(text: str, alert=None) -> bool:
        captured.update(text=text, alert=alert)
        return True

    monkeypatch.setattr(alerts, "deliver_alert", fake_send)
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 200
    text = str(captured["text"])
    assert "⚪ 错单复盘｜沪金 AU2606｜降级" in text
    assert "回归窗口内未回归价差中枢，更接近行情重定价，不计为错单" in text


def test_historical_analysis_uses_interactive_card(monkeypatch) -> None:
    body = {
        "event_id": "history:AU:2026-04-01:2026-07-22:analysis:20260723T080000",
        "kind": "historical_complete",
        "occurred_at": "2026-07-23T08:00:00+08:00",
        "source": "historical",
        "severity": "info",
        "symbol": "AU",
        "summary": "分行后的文本兜底",
        "historical_analysis": {
            "schema_version": 1,
            "product_code": "AU",
            "product_name": "沪金 AU",
            "period_start": "2026-04-01",
            "period_end": "2026-07-22",
            "contract_count": 1,
            "event_count": 100,
            "focus_event_count": 1,
            "active_contract_days": 80,
            "eligible_seconds": 100000,
            "maximum_deviation_percent": 16.65,
            "maximum_event": {
                "symbol": "SHFE.au2606",
                "occurred_at": "2026-05-19T21:04:35+08:00",
                "deviation_percent": 16.65,
                "direction": "down",
            },
            "overall_buckets": [
                {"label": "<1%", "count": 99, "share_percent": 99.0},
                {"label": "16%-17%", "count": 1, "share_percent": 1.0},
            ],
            "zoom_buckets": [],
            "contracts": [
                {
                    "symbol": "SHFE.au2606",
                    "total_events": 100,
                    "active_days": 80,
                    "eligible_seconds": 100000,
                    "events_per_10000_seconds": 10.0,
                    "active_days_per_event": 0.8,
                    "maximum_deviation_percent": 16.65,
                    "up_count": 45,
                    "down_count": 55,
                    "buckets": [],
                }
            ],
            "charts": [
                {
                    "name": "distribution.png",
                    "content_type": "image/png",
                    "caption": "总体分布",
                    "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode(),
                }
            ],
            "report_files": ["wrong_price_analysis.html"],
        },
    }
    captured: dict[str, object] = {}
    client = _client(monkeypatch)

    def fake_send(text: str, alert=None) -> bool:
        captured.update(text=text, alert=alert)
        return True

    monkeypatch.setattr(alerts, "deliver_alert", fake_send)
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )

    assert response.status_code == 200
    notification = alerts.build_alert_notification(captured["alert"], str(captured["text"]))
    assert notification.title == "沪金 AU｜历史错价回溯完成"
    # 图必须进 images 并被某个段落引用，否则会在渲染时被静默丢掉
    assert len(notification.images) == 1
    assert any(segment.kind == "image" for segment in notification.segments)
    stored = alerts._stored_alert_payload(captured["alert"])
    stored_chart = stored["historical_analysis"]["charts"][0]
    assert "data_base64" not in stored_chart


def _wrong_price_with_chart() -> dict[str, object]:
    """监控端 history.py 实际发出的形状：顶层 charts，不带 historical_analysis。"""
    body = _payload("wrong_price_detected")
    body.update(
        {
            "trigger_market": "SHFE_AU_1S_LOW",
            "trigger_price": 995.6,
            "trigger_tick_id": "3",
            "volume_delta": 12.0,
            "deviation_percent": 16.65,
            "clock_skew_ms": 120.0,
            "book_breach_cny_per_g": 164.5,
            "charts": [
                {
                    "name": "au2606-20260519-210435.png",
                    "content_type": "image/png",
                    "caption": "错单成交明细：价格与盘口 / 成交量 / 横向孤立度 / 成交事实",
                    "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode(),
                }
            ],
        }
    )
    return body


def test_wrong_price_alert_with_chart_goes_out_as_card(monkeypatch) -> None:
    """带图的逐笔错单必须走卡片而不是纯文本，否则图会被静默丢掉。"""
    captured: dict[str, object] = {}
    client = _client(monkeypatch)

    def fake_send(text: str, alert=None) -> bool:
        captured.update(text=text, alert=alert)
        return True

    monkeypatch.setattr(alerts, "deliver_alert", fake_send)
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=_wrong_price_with_chart(),
    )

    assert response.status_code == 200
    alert = captured["alert"]
    assert len(alert.charts) == 1
    notification = alerts.build_alert_notification(alert, str(captured["text"]))
    assert notification.title == "🔴 疑似错单成交｜沪金 AU2606"
    assert notification.level == "warn"  # severity=warning
    # 图必须进 images 且被段落引用，否则渲染时会被静默丢掉
    assert len(notification.images) == 1
    assert any(segment.kind == "image" for segment in notification.segments)
    # 正文必须按预格式化走：排版里有 ｜ 和 *，交给 markdown 会被重排
    body = [seg for seg in notification.segments if seg.kind == "text" and seg.preformatted]
    assert body and "极值成交价：995.60" in body[0].text


def test_alert_chart_bytes_are_not_stored(monkeypatch) -> None:
    """顶层 charts 也要剥 base64，否则 payload_json 每条多存几十万字符。"""
    alert = alerts.GoldSpreadAlert.model_validate(_wrong_price_with_chart())
    stored = alerts._stored_alert_payload(alert)
    chart = stored["charts"][0]
    assert "data_base64" not in chart
    assert chart["base64_characters"] > 0


def test_charts_and_historical_analysis_cannot_be_combined(monkeypatch) -> None:
    body = _wrong_price_with_chart()
    body["historical_analysis"] = {"schema_version": 1}
    response = _client(monkeypatch).post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 422


def test_alert_without_chart_still_goes_out_as_text(monkeypatch) -> None:
    captured: dict[str, object] = {}
    client = _client(monkeypatch)
    monkeypatch.setattr(
        alerts,
        "deliver_alert",
        lambda text, alert=None: captured.update(text=text) or True,
    )
    body = _wrong_price_with_chart()
    body.pop("charts")
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 200
    assert "疑似错单成交" in str(captured["text"])


def test_card_delivery_failure_falls_back_to_text(monkeypatch) -> None:
    """上传图失败不能让告警整条丢掉——图没了，字必须还在。

    收敛后降级发生在 channel 层（feishu.send 卡片被拒时退回纯文本），
    这里守的是上游前提：交给中枢的消息本身带着完整正文，
    因此即便一张图都传不上去，plain_text() 仍是完整告警。
    """
    captured: dict[str, object] = {}
    client = _client(monkeypatch)
    monkeypatch.setattr(
        alerts,
        "deliver_alert",
        lambda text, alert=None: captured.update(text=text) or True,
    )
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=_wrong_price_with_chart(),
    )
    assert response.status_code == 200
    assert "疑似错单成交" in str(captured["text"])


def test_non_png_chart_is_rejected_at_the_model(monkeypatch) -> None:
    """非 PNG 必须在构造消息时就被拒，不能一路带到 IM API 才报错。

    收敛前这层校验在 _upload_charts 里；现在搬到了 NotifyImage，
    三个通道共用同一道闸门。
    """
    body = _wrong_price_with_chart()
    body["charts"][0]["data_base64"] = base64.b64encode(b"GIF89a not a png").decode()
    alert = alerts.GoldSpreadAlert.model_validate(body)
    with pytest.raises(ValidationError, match="PNG"):
        alerts.build_alert_notification(alert, "标题\n正文")


def test_data_silence_alert_is_accepted(monkeypatch) -> None:
    """监控端 2026-07-30 14:22 实际发出、被 422 退回的报文原样重放。"""
    captured: dict[str, object] = {}
    client = _client(monkeypatch)
    monkeypatch.setattr(
        alerts,
        "deliver_alert",
        lambda text, alert=None: captured.update(text=text) or True,
    )
    body = {
        "event_id": "mt5:data_silence:20260730T141432",
        "kind": "data_silence",
        "occurred_at": "2026-07-30T14:22:04+08:00",
        "source": "live",
        "severity": "critical",
        "summary": "MT5 XAUUSD/USDCNH 已静默约 370 秒（自 07-30 14:14:32），价差与异常判定当前失效。",
    }
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 200
    assert "行情断流" in str(captured["text"])
    assert "已静默约 370 秒" in str(captured["text"])


def test_data_silence_recovered_alert_is_accepted(monkeypatch) -> None:
    captured: dict[str, object] = {}
    client = _client(monkeypatch)
    monkeypatch.setattr(
        alerts,
        "deliver_alert",
        lambda text, alert=None: captured.update(text=text) or True,
    )
    body = {
        "event_id": "mt5:data_silence_recovered:20260730T141432",
        "kind": "data_silence_recovered",
        "occurred_at": "2026-07-30T17:03:38+08:00",
        "source": "live",
        "severity": "warning",
        "summary": "MT5 行情已恢复，静默自 07-30 14:14:32 起，持续约 10146 秒。",
    }
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 200
    assert "行情已恢复" in str(captured["text"])


def test_replay_summary_accepts_replay_source(monkeypatch) -> None:
    """replay 链路的 kind 和 source 都在旧白名单外，两处都要放开。"""
    captured: dict[str, object] = {}
    client = _client(monkeypatch)
    monkeypatch.setattr(
        alerts,
        "deliver_alert",
        lambda text, alert=None: captured.update(text=text) or True,
    )
    body = {
        "event_id": "replay:20260730T023815",
        "kind": "replay_summary",
        "occurred_at": "2026-07-30T02:38:15+08:00",
        "source": "replay",
        "severity": "critical",
        "summary": "收盘复盘完成。2026-07-27 复盘失败：HTTPError: HTTP Error 429: Too Many Requests",
    }
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 200
    assert "收盘复盘" in str(captured["text"])


def test_replay_summary_builds_structured_progress_and_report_segments(monkeypatch) -> None:
    body = {
        "event_id": "remote-replay:job-123:succeeded",
        "kind": "replay_summary",
        "occurred_at": "2026-08-31T11:00:00+08:00",
        "source": "replay",
        "severity": "warning",
        "summary": "正式复盘完成",
        "replay_progress": {
            "job_id": "job-123",
            "status_code": "SUCCEEDED｜作业成功",
            "phase": "complete",
            "completed_partitions": 97,
            "total_partitions": 97,
            "worker_process_count": 0,
            "started_at": "2026-08-31T04:00:55+08:00",
            "latest_artifact_at": "2026-08-31T10:58:00+08:00",
            "elapsed_seconds": 25085,
            "estimated_remaining_seconds": 0,
            "result_file_count": 996,
            "report_metrics": [
                {
                    "metric": "physical_fill_count｜物理成交数",
                    "count": 2969,
                    "denominator": 3957,
                },
                {
                    "metric": "net_profitable_position_count｜真实平仓净盈利持仓数",
                    "count": 2563,
                    "denominator": 3586,
                },
            ],
        },
    }
    alert = alerts.GoldSpreadAlert.model_validate(body)
    text = alerts.render_gold_spread_alert(alert)
    notification = alerts.build_alert_notification(alert, text)

    assert notification.title == "远端正式复盘、审计与报告已完成"
    field_segments = [segment for segment in notification.segments if segment.kind == "fields"]
    rendered_fields = {
        field.name: field.value
        for segment in field_segments
        for field in segment.fields
    }
    assert rendered_fields["作业"] == "job-123"
    assert rendered_fields["复盘进度"] == "97 / 97 个交易日分区（100.0%）"
    assert rendered_fields["已运行"] == "6 小时 58 分钟"
    assert rendered_fields["预计剩余"] == "0 分钟"
    assert rendered_fields["物理成交数"] == "2,969 / 3,957"
    assert rendered_fields["真实平仓净盈利持仓数"] == "2,563 / 3,586"
    assert rendered_fields["结果清单"] == "996 个已校验文件"


def test_replay_failure_truncates_long_error_to_notification_field_limit() -> None:
    alert = alerts.GoldSpreadAlert.model_validate(
        {
            "event_id": "remote-replay:job-123:failed",
            "kind": "replay_summary",
            "occurred_at": "2026-08-31T11:00:00+08:00",
            "source": "replay",
            "severity": "critical",
            "summary": "正式复盘失败",
            "replay_progress": {
                "job_id": "job-123",
                "status_code": "FAILED｜作业失败",
                "phase": "global_audit",
                "completed_partitions": 97,
                "total_partitions": 97,
                "worker_process_count": 0,
                "error_type": "RuntimeError",
                "error_message": "x" * 900,
            },
        }
    )

    notification = alerts.build_alert_notification(
        alert, alerts.render_gold_spread_alert(alert)
    )
    failure = next(
        field.value
        for segment in notification.segments
        if segment.kind == "fields"
        for field in segment.fields
        if field.name == "失败详情"
    )
    assert len(failure) == 512


def test_gold_endpoint_returns_unified_delivery_receipt(monkeypatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setattr(
        alerts,
        "deliver_alert",
        lambda *_: {
            "outbox_id": 42,
            "duplicate": False,
            "targets": 1,
            "sent": 1,
            "pending": 0,
            "dead": 0,
            "failed": 0,
        },
    )

    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=_payload(),
    )

    assert response.status_code == 200
    assert response.json()["outbox_id"] == 42
    assert response.json()["targets"] == 1
    assert response.json()["sent"] == 1


def test_unknown_kind_still_rejected(monkeypatch) -> None:
    body = {
        "event_id": "mt5:bogus_kind:20260730T141432",
        "kind": "bogus_kind",
        "occurred_at": "2026-07-30T14:22:04+08:00",
        "source": "live",
        "severity": "critical",
        "summary": "x",
    }
    response = _client(monkeypatch).post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )
    assert response.status_code == 422
