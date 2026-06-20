"""Backend tests for the Feishu raw webhook receiver.

Covers URL-verification challenge, plain message events, encrypted payload,
signature verification, and verification-token gating. No production secrets
or live Feishu calls.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def _aes_encrypt(plaintext: str, encrypt_key: str) -> str:
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = bytes(range(16))
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(plaintext.encode("utf-8"), AES.block_size)
    encrypted = iv + cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode("ascii")


def _build_message_event(
    *,
    event_id: str = "evt-1",
    message_id: str = "om_dc13",
    chat_id: str = "oc_group_1",
    chat_type: str = "group",
    open_id: str = "ou_user_1",
    sender_name: str = "hao",
    mentions: list[dict[str, Any]] | None = None,
    content: str = '{"text":"hello"}',
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant-x",
            "create_time": "1760000000000",
            "token": "",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": open_id, "user_id": "u_1"},
                "sender_name": sender_name,
            },
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": "text",
                "content": content,
                "mentions": mentions or [],
            },
        },
    }


class FeishuWebhookTests(unittest.TestCase):
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
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "FEISHU_EVENT_SPOOL_DIR",
                "FEISHU_ENCRYPT_KEY",
                "FEISHU_WEBHOOK_ENCRYPT_KEY",
                "FEISHU_VERIFICATION_TOKEN",
                "FEISHU_WEBHOOK_VERIFICATION_TOKEN",
                "FEISHU_WEBHOOK_REQUIRE_SIGNATURE",
            )
        }
        for key in self._saved_env:
            os.environ.pop(key, None)
        os.environ["FEISHU_EVENT_SPOOL_DIR"] = self._tmp.name
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _spool_files(self, suffix: str) -> list[dict[str, Any]]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(Path(self._tmp.name).glob(f"*-{suffix}.json"))
        ]

    def test_url_verification_challenge_returns_echo(self) -> None:
        body = {"type": "url_verification", "challenge": "abc-123", "token": ""}
        resp = self.client.post("/v1/webhooks/feishu", json=body)
        self.assertEqual(200, resp.status_code)
        self.assertEqual({"challenge": "abc-123"}, resp.json())
        spooled = self._spool_files("url-verification")
        self.assertEqual(1, len(spooled))
        self.assertEqual("abc-123", spooled[0]["challenge"])

    def test_plain_group_message_event_is_parsed_and_spooled(self) -> None:
        event = _build_message_event(
            chat_type="group",
            mentions=[{"id": {"open_id": "ou_bot"}, "name": "Bot"}],
        )
        resp = self.client.post("/v1/webhooks/feishu", json=event)
        self.assertEqual(200, resp.status_code)
        self.assertEqual({"status": "ok"}, resp.json())
        spooled = self._spool_files("event")
        self.assertEqual(1, len(spooled))
        record = spooled[0]
        self.assertEqual("evt-1", record["event_id"])
        self.assertEqual("group", record["chat_type"])
        self.assertEqual("oc_group_1", record["chat_id"])
        self.assertEqual("om_dc13", record["message_id"])
        self.assertEqual("ou_user_1", record["sender_open_id"])
        self.assertEqual(True, record["is_group"])
        self.assertEqual([{"open_id": "ou_bot", "user_id": "", "name": "Bot"}], record["mentions"])

    def test_plain_private_message_event_marks_is_group_false(self) -> None:
        event = _build_message_event(chat_type="p2p", chat_id="oc_p2p_1", mentions=[])
        resp = self.client.post("/v1/webhooks/feishu", json=event)
        self.assertEqual(200, resp.status_code)
        record = self._spool_files("event")[0]
        self.assertEqual("p2p", record["chat_type"])
        self.assertEqual(False, record["is_group"])
        self.assertEqual([], record["mentions"])

    def test_non_message_event_is_spooled_as_other(self) -> None:
        body = {
            "schema": "2.0",
            "header": {"event_id": "evt-x", "event_type": "im.chat.member.user.added_v1"},
            "event": {},
        }
        resp = self.client.post("/v1/webhooks/feishu", json=body)
        self.assertEqual(200, resp.status_code)
        spooled = self._spool_files("event-other")
        self.assertEqual(1, len(spooled))
        self.assertEqual("im.chat.member.user.added_v1", spooled[0]["event_type"])
        self.assertEqual("evt-x", spooled[0]["event_id"])

    def test_encrypted_payload_is_decrypted_and_handled(self) -> None:
        encrypt_key = "test-feishu-encrypt-key"
        os.environ["FEISHU_ENCRYPT_KEY"] = encrypt_key
        plain = _build_message_event(event_id="evt-enc", message_id="om_enc")
        body = {"encrypt": _aes_encrypt(json.dumps(plain), encrypt_key)}
        resp = self.client.post("/v1/webhooks/feishu", json=body)
        self.assertEqual(200, resp.status_code)
        record = self._spool_files("event")[0]
        self.assertEqual("evt-enc", record["event_id"])
        self.assertEqual("om_enc", record["message_id"])

    def test_encrypted_payload_without_encrypt_key_is_rejected(self) -> None:
        body = {"encrypt": "anything"}
        resp = self.client.post("/v1/webhooks/feishu", json=body)
        self.assertEqual(400, resp.status_code)

    def test_signature_required_and_valid_accepted(self) -> None:
        encrypt_key = "test-feishu-encrypt-key"
        os.environ["FEISHU_ENCRYPT_KEY"] = encrypt_key
        os.environ["FEISHU_WEBHOOK_REQUIRE_SIGNATURE"] = "true"
        plain = _build_message_event(event_id="evt-sig")
        encrypted_body = json.dumps({"encrypt": _aes_encrypt(json.dumps(plain), encrypt_key)}).encode("utf-8")
        timestamp = "1760000000"
        nonce = "nonce-1"
        digest = hashlib.sha256(
            timestamp.encode("utf-8")
            + nonce.encode("utf-8")
            + encrypt_key.encode("utf-8")
            + encrypted_body
        ).hexdigest()
        resp = self.client.post(
            "/v1/webhooks/feishu",
            content=encrypted_body,
            headers={
                "content-type": "application/json",
                "x-lark-request-timestamp": timestamp,
                "x-lark-request-nonce": nonce,
                "x-lark-signature": digest,
            },
        )
        self.assertEqual(200, resp.status_code)
        self.assertEqual("evt-sig", self._spool_files("event")[0]["event_id"])

    def test_signature_required_and_invalid_rejected(self) -> None:
        encrypt_key = "test-feishu-encrypt-key"
        os.environ["FEISHU_ENCRYPT_KEY"] = encrypt_key
        os.environ["FEISHU_WEBHOOK_REQUIRE_SIGNATURE"] = "true"
        plain = _build_message_event()
        encrypted_body = json.dumps({"encrypt": _aes_encrypt(json.dumps(plain), encrypt_key)}).encode("utf-8")
        resp = self.client.post(
            "/v1/webhooks/feishu",
            content=encrypted_body,
            headers={
                "content-type": "application/json",
                "x-lark-request-timestamp": "1760000000",
                "x-lark-request-nonce": "nonce-1",
                "x-lark-signature": "bad-signature",
            },
        )
        self.assertEqual(401, resp.status_code)
        self.assertEqual([], self._spool_files("event"))

    def test_verification_token_mismatch_rejected(self) -> None:
        os.environ["FEISHU_VERIFICATION_TOKEN"] = "expected-token"
        body = _build_message_event()
        body["header"]["token"] = "wrong-token"
        resp = self.client.post("/v1/webhooks/feishu", json=body)
        self.assertEqual(401, resp.status_code)
        self.assertEqual([], self._spool_files("event"))

    def test_verification_token_match_accepted(self) -> None:
        os.environ["FEISHU_VERIFICATION_TOKEN"] = "expected-token"
        body = _build_message_event()
        body["header"]["token"] = "expected-token"
        resp = self.client.post("/v1/webhooks/feishu", json=body)
        self.assertEqual(200, resp.status_code)
        self.assertEqual("evt-1", self._spool_files("event")[0]["event_id"])

    def test_invalid_json_body_returns_400(self) -> None:
        resp = self.client.post(
            "/v1/webhooks/feishu",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(400, resp.status_code)


if __name__ == "__main__":
    unittest.main()
