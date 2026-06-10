from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class ChanjetTokenAutoRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))

        from app.integrations.chanjet import handlers

        cls.handlers = handlers

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_env = {
            key: os.environ.get(key)
            for key in ("CHANJET_EVENT_SPOOL_DIR", "CHANJET_CERTIFICATE", "CHANJET_OPEN_TOKEN_FILE")
        }
        os.environ["CHANJET_EVENT_SPOOL_DIR"] = self._tmp.name
        self.token_file = os.path.join(self._tmp.name, "token", "chanjet_open_token.txt")

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _app_ticket_payload(self) -> dict:
        return {
            "id": "evt-ticket",
            "msgType": "APP_TICKET",
            "bizContent": {"appTicket": "ticket-abc"},
        }

    def test_app_ticket_event_exchanges_and_writes_token_file(self) -> None:
        os.environ["CHANJET_CERTIFICATE"] = "cert-xyz"
        os.environ["CHANJET_OPEN_TOKEN_FILE"] = self.token_file

        with patch.object(
            self.handlers,
            "generate_self_built_open_token",
            return_value={"value": {"openToken": "eyJ-new-token"}},
        ) as exchange:
            result = self.handlers.handle_chanjet_webhook(self._app_ticket_payload())

        self.assertEqual({"result": "success"}, result)
        exchange.assert_called_once_with("ticket-abc", "cert-xyz")
        self.assertEqual("eyJ-new-token", Path(self.token_file).read_text(encoding="utf-8").strip())
        refresh_files = list(Path(self._tmp.name).glob("*-token-refresh.json"))
        self.assertEqual(1, len(refresh_files))

    def test_no_certificate_skips_exchange(self) -> None:
        os.environ.pop("CHANJET_CERTIFICATE", None)
        os.environ["CHANJET_OPEN_TOKEN_FILE"] = self.token_file

        with patch.object(self.handlers, "generate_self_built_open_token") as exchange:
            result = self.handlers.handle_chanjet_webhook(self._app_ticket_payload())

        self.assertEqual({"result": "success"}, result)
        exchange.assert_not_called()
        self.assertFalse(Path(self.token_file).exists())

    def test_non_ticket_event_skips_exchange(self) -> None:
        os.environ["CHANJET_CERTIFICATE"] = "cert-xyz"
        os.environ["CHANJET_OPEN_TOKEN_FILE"] = self.token_file

        with patch.object(self.handlers, "generate_self_built_open_token") as exchange:
            result = self.handlers.handle_chanjet_webhook(
                {"id": "evt-bom", "msgType": "Bom_Update", "bizContent": {"Code": "X"}}
            )

        self.assertEqual({"result": "success"}, result)
        exchange.assert_not_called()

    def test_exchange_failure_keeps_webhook_success_and_spools_error(self) -> None:
        os.environ["CHANJET_CERTIFICATE"] = "cert-xyz"
        os.environ["CHANJET_OPEN_TOKEN_FILE"] = self.token_file

        with patch.object(
            self.handlers,
            "generate_self_built_open_token",
            side_effect=RuntimeError("HTTP Error 401"),
        ):
            result = self.handlers.handle_chanjet_webhook(self._app_ticket_payload())

        self.assertEqual({"result": "success"}, result)
        error_files = list(Path(self._tmp.name).glob("*-token-refresh-error.json"))
        self.assertEqual(1, len(error_files))
        saved = json.loads(error_files[0].read_text(encoding="utf-8"))
        self.assertIn("401", saved["error"])

    def test_extract_open_token_handles_nested_and_flat_responses(self) -> None:
        extract = self.handlers._extract_open_token
        self.assertEqual("t1", extract({"openToken": "t1"}))
        self.assertEqual("t2", extract({"result": {"accessToken": "t2"}}))
        self.assertEqual("t3", extract({"data": {"token": "t3"}}))
        self.assertEqual("", extract({"code": "0", "message": "ok"}))


if __name__ == "__main__":
    unittest.main()
