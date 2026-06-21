from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))


class BackendWebhookGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))

        from app.main import app

        cls.app = app

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_spool = os.environ.get("CHANJET_EVENT_SPOOL_DIR")
        self._old_aes_key = os.environ.get("CHANJET_WEBHOOK_AES_KEY")
        os.environ["CHANJET_EVENT_SPOOL_DIR"] = self._tmp.name

    def tearDown(self) -> None:
        if self._old_spool is None:
            os.environ.pop("CHANJET_EVENT_SPOOL_DIR", None)
        else:
            os.environ["CHANJET_EVENT_SPOOL_DIR"] = self._old_spool
        if self._old_aes_key is None:
            os.environ.pop("CHANJET_WEBHOOK_AES_KEY", None)
        else:
            os.environ["CHANJET_WEBHOOK_AES_KEY"] = self._old_aes_key
        self._tmp.cleanup()

    def _call_post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        for route in self.app.routes:
            if getattr(route, "path", "") == path and "POST" in getattr(route, "methods", set()):
                return route.endpoint(payload or {})
        self.fail(f"missing POST route: {path}")

    def _call_get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        for route in self.app.routes:
            if getattr(route, "path", "") == path and "GET" in getattr(route, "methods", set()):
                return route.endpoint(**kwargs)
        self.fail(f"missing GET route: {path}")

    def test_chanjet_webhook_returns_chanjet_success_contract(self) -> None:
        result = self._call_post("/v1/webhooks/chanjet", {"event": "ping"})

        self.assertEqual({"result": "success"}, result)

    def test_chanjet_webhook_passes_decoded_event_to_optional_sink(self) -> None:
        from app.integrations.chanjet.handlers import handle_chanjet_webhook

        received = []

        result = handle_chanjet_webhook(
            {
                "id": "evt-bom",
                "msgType": "Bom_Update",
                "bizContent": {"Code": "HYD-4197PC", "Version": "2026-06-03F"},
            },
            event_sink=lambda event, record: received.append((event, record)),
        )

        self.assertEqual({"result": "success"}, result)
        self.assertEqual(1, len(received))
        self.assertEqual("Bom_Update", received[0][0].msg_type)
        self.assertEqual("HYD-4197PC", received[0][0].biz_content["Code"])

    def test_chanjet_webhook_decrypts_aes_payload_and_spools_event(self) -> None:
        key = "1234567890123456"
        os.environ["CHANJET_WEBHOOK_AES_KEY"] = key
        event = {
            "id": "evt-1",
            "appKey": "demo-app",
            "appId": "45057",
            "msgType": "APP_TICKET",
            "time": "1760000000000",
            "bizContent": {"appTicket": "ticket-for-test"},
        }
        cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
        encrypted = base64.b64encode(
            cipher.encrypt(pad(json.dumps(event).encode("utf-8"), AES.block_size))
        ).decode("ascii")

        result = self._call_post("/v1/webhooks/chanjet", {"encryptMsg": encrypted})

        self.assertEqual({"result": "success"}, result)
        files = list(Path(self._tmp.name).glob("*-event.json"))
        self.assertEqual(1, len(files))
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual("APP_TICKET", saved["msg_type"])
        self.assertEqual("ticket-for-test", saved["biz_content"]["appTicket"])

    def test_chanjet_oauth_callback_spools_code_without_exchanging_by_default(self) -> None:
        result = self._call_get(
            "/v1/webhooks/chanjet/oauth",
            code="code-for-test",
            state="state-for-test",
            redirect_uri="https://example.com/api/v1/webhooks/chanjet/oauth",
        )

        self.assertEqual(True, result["code_received"])
        files = list(Path(self._tmp.name).glob("*-oauth.json"))
        self.assertEqual(1, len(files))
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(True, saved["code_received"])
        self.assertEqual("state-for-test", saved["state"])
        self.assertNotIn("token_response", saved)

    def test_wecom_webhook_placeholder_response(self) -> None:
        result = self._call_post("/v1/webhooks/wecom", {"event": "ping"})

        self.assertEqual("received", result["status"])
        self.assertEqual("wecom", result["provider"])
        self.assertEqual("placeholder", result["mode"])

    # Feishu webhook moved off the placeholder router; real coverage lives in
    # tests/test_backend_feishu_webhook.py (URL challenge, encrypted payload,
    # signature, verification token).


if __name__ == "__main__":
    unittest.main()
