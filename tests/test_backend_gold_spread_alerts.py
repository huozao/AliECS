from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

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
    monkeypatch.setattr(alerts, "send_feishu_text", lambda *_, **__: sent)
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

    def fake_send(receive_id: str, text: str, **kwargs) -> bool:
        captured.update(receive_id=receive_id, text=text, **kwargs)
        return True

    monkeypatch.setattr(alerts, "send_feishu_text", fake_send)
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=_payload(),
    )
    assert response.status_code == 200
    assert captured["receive_id"] == "oc_target"
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

    def fake_send(receive_id: str, text: str, **kwargs) -> bool:
        captured.update(receive_id=receive_id, text=text, **kwargs)
        return True

    monkeypatch.setattr(alerts, "send_feishu_text", fake_send)
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

    def fake_send(receive_id: str, text: str, **kwargs) -> bool:
        captured.update(receive_id=receive_id, text=text, **kwargs)
        return True

    monkeypatch.setattr(alerts, "send_feishu_text", fake_send)
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

    def fake_send(receive_id: str, text: str, **kwargs) -> bool:
        captured.update(receive_id=receive_id, text=text, **kwargs)
        return True

    monkeypatch.setattr(alerts, "send_feishu_text", fake_send)
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

    def fake_send(receive_id: str, text: str, **kwargs) -> bool:
        captured.update(receive_id=receive_id, text=text, **kwargs)
        return True

    monkeypatch.setattr(alerts, "send_feishu_text", fake_send)
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

    def fake_card(receive_id: str, alert, **kwargs) -> bool:
        captured.update(receive_id=receive_id, alert=alert, **kwargs)
        return True

    monkeypatch.setattr(alerts, "send_feishu_historical_card", fake_card)
    monkeypatch.setattr(alerts, "send_feishu_text", lambda *_, **__: False)
    response = client.post(
        "/v1/internal/gold-spread/alerts",
        headers={"X-Gold-Spread-Token": "test-token"},
        json=body,
    )

    assert response.status_code == 200
    assert captured["receive_id"] == "oc_target"
    card = alerts.build_historical_report_card(captured["alert"], ["img_key_1"])
    assert card["header"]["title"]["content"] == "沪金 AU｜历史错价回溯完成"
    assert any(element.get("tag") == "img" for element in card["elements"])
    stored = alerts._stored_alert_payload(captured["alert"])
    stored_chart = stored["historical_analysis"]["charts"][0]
    assert "data_base64" not in stored_chart
