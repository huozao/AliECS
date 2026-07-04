from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class WecomBCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        from app.routers import ops as main_module

        cls.main = main_module

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]

    def _payload(self) -> dict:
        return {
            "cmd": "aibot_msg_callback",
            "body": {
                "msgid": "msg-1",
                "aibotid": "aibQWX",
                "chatid": "room-1",
                "chattype": "group",
                "from": {"userid": "user-1"},
                "msgtype": "text",
                "text": {"content": "@机器人 你好"},
            },
        }

    def test_wecom_b_capture_message_persists_forward_message(self) -> None:
        conn = FakeConn()
        old_conn = self.main._conn
        self.main._conn = lambda: conn
        try:
            with patch.dict(os.environ, {"WECOM_B_CAPTURE_TOKEN": "s3cret"}, clear=False):
                result = self.main.wecom_b_capture_message(
                    self._payload(), x_wecom_capture_token="s3cret"
                )
        finally:
            self.main._conn = old_conn

        self.assertEqual({"status": "received", "msg_id": "msg-1"}, result)
        self.assertEqual("msg-1", conn.params[0])
        self.assertEqual("aibQWX", conn.params[1])
        self.assertEqual("room-1", conn.params[2])
        self.assertEqual("group", conn.params[3])
        self.assertEqual("user-1", conn.params[4])
        self.assertEqual("text", conn.params[5])
        self.assertEqual("@机器人 你好", conn.params[6])
        self.assertTrue(conn.committed)

    def test_capture_disabled_when_token_unset(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "WECOM_B_CAPTURE_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                self.main.wecom_b_capture_message(self._payload(), x_wecom_capture_token="anything")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_capture_rejects_wrong_token(self) -> None:
        with patch.dict(os.environ, {"WECOM_B_CAPTURE_TOKEN": "s3cret"}, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                self.main.wecom_b_capture_message(self._payload(), x_wecom_capture_token="wrong")
        self.assertEqual(ctx.exception.status_code, 403)


class FakeConn:
    def __init__(self) -> None:
        self.params = None
        self.committed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self.sql = sql
        self.params = params

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
