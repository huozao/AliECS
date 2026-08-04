from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def _token_expiring_in(delta: timedelta) -> str:
    exp = int((datetime.now(timezone.utc) + delta).timestamp())
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class ChanjetTokenAlertTests(unittest.TestCase):
    """openToken 有效期只有 6 天且全靠 webhook 续期，掉到阈值以下即说明链路已断。"""

    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        from app.integrations import events
        from app.routers import ops

        cls.ops = ops
        cls.events = events

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]

    def setUp(self) -> None:
        self._old_env = os.environ.get("CHANJET_OPEN_TOKEN_FILE")

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("CHANJET_OPEN_TOKEN_FILE", None)
        else:
            os.environ["CHANJET_OPEN_TOKEN_FILE"] = self._old_env

    def _status_for(self, token: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "chanjet_open_token.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(token)
            os.environ["CHANJET_OPEN_TOKEN_FILE"] = path
            return self.ops._chanjet_token_status()

    def test_fresh_token_is_ok(self) -> None:
        status = self._status_for(_token_expiring_in(timedelta(days=5, hours=23)))
        self.assertTrue(status["ok"])
        self.assertFalse(status["expired"])

    def test_below_four_days_is_not_ok(self) -> None:
        status = self._status_for(_token_expiring_in(timedelta(days=3, hours=23)))
        self.assertFalse(status["ok"])
        self.assertFalse(status["expired"])

    def test_expired_token_flagged(self) -> None:
        status = self._status_for(_token_expiring_in(timedelta(hours=-1)))
        self.assertFalse(status["ok"])
        self.assertTrue(status["expired"])
        self.assertIn("已失效", status["message"])

    def test_unparsable_token_is_not_ok(self) -> None:
        status = self._status_for("not-a-jwt")
        self.assertFalse(status["ok"])

    def test_empty_token_file_is_not_ok(self) -> None:
        status = self._status_for("")
        self.assertFalse(status["ok"])

    def test_missing_config_is_not_alerted(self) -> None:
        os.environ.pop("CHANJET_OPEN_TOKEN_FILE", None)
        status = self.ops._chanjet_token_status()
        self.assertFalse(status["configured"])
        self.assertTrue(status["ok"])

    def test_missing_file_is_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CHANJET_OPEN_TOKEN_FILE"] = os.path.join(tmp, "absent.txt")
            status = self.ops._chanjet_token_status()
        self.assertFalse(status["ok"])

    def test_attention_item_raised_when_degraded(self) -> None:
        items = self.events.build_ops_attention_items(
            {"chanjet_token": {"configured": True, "ok": False, "expired": True, "message": "openToken 已失效"}}
        )
        codes = [item["code"] for item in items]
        self.assertIn("chanjet_token_expiring", codes)
        item = next(i for i in items if i["code"] == "chanjet_token_expiring")
        self.assertEqual(item["level"], "critical")

    def test_attention_item_warning_when_only_expiring(self) -> None:
        items = self.events.build_ops_attention_items(
            {"chanjet_token": {"configured": True, "ok": False, "expired": False, "message": "剩余 80.0 小时"}}
        )
        item = next(i for i in items if i["code"] == "chanjet_token_expiring")
        self.assertEqual(item["level"], "warning")

    def test_no_attention_item_when_healthy(self) -> None:
        items = self.events.build_ops_attention_items(
            {"chanjet_token": {"configured": True, "ok": True, "expired": False, "message": "剩余 140.0 小时"}}
        )
        self.assertNotIn("chanjet_token_expiring", [item["code"] for item in items])

    def test_alert_not_sent_when_healthy(self) -> None:
        sent: list = []
        original = self.ops._send_chanjet_token_alert
        self.ops._send_chanjet_token_alert = lambda status: sent.append(status) or True
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "token.txt")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(_token_expiring_in(timedelta(days=5)))
                os.environ["CHANJET_OPEN_TOKEN_FILE"] = path
                result = self.ops.chanjet_token_alert_once()
        finally:
            self.ops._send_chanjet_token_alert = original
        self.assertFalse(result["alerted"])
        self.assertEqual(sent, [])

    def test_alert_sent_every_check_when_degraded(self) -> None:
        """不做去重：坏着就每轮都发，直到修复。"""
        sent: list = []
        original = self.ops._send_chanjet_token_alert
        self.ops._send_chanjet_token_alert = lambda status: sent.append(status) or True
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "token.txt")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(_token_expiring_in(timedelta(days=1)))
                os.environ["CHANJET_OPEN_TOKEN_FILE"] = path
                first = self.ops.chanjet_token_alert_once()
                second = self.ops.chanjet_token_alert_once()
        finally:
            self.ops._send_chanjet_token_alert = original
        self.assertTrue(first["alerted"])
        self.assertTrue(second["alerted"])
        self.assertEqual(len(sent), 2)

    def test_alert_text_carries_recovery_steps(self) -> None:
        text = self.ops._chanjet_token_alert_text(
            {"expired": True, "message": "openToken 已失效", "expires_at": "2026-08-10T03:57:00+00:00"}
        )
        self.assertIn("重置消息地址状态并发送AppTicket", text)
        self.assertIn("runbooks/tplus.md", text)


if __name__ == "__main__":
    unittest.main()
