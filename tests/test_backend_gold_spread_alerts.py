from __future__ import annotations

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
