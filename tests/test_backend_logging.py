from __future__ import annotations

import io
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_logging_utils():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import logging_utils

    return logging_utils


class JsonFormatterTests(unittest.TestCase):
    def test_format_emits_json_with_core_fields(self) -> None:
        logging_utils = load_logging_utils()
        formatter = logging_utils.JsonFormatter()
        record = logging.LogRecord(
            name="aliecs.request",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="request completed",
            args=None,
            exc_info=None,
        )
        record.request_id = "req-123"
        record.method = "GET"
        record.path = "/healthz"
        record.status_code = 200
        record.duration_ms = 1.5
        record.user = "tester"

        line = formatter.format(record)
        data = json.loads(line)

        self.assertEqual(data["message"], "request completed")
        self.assertEqual(data["level"], "INFO")
        self.assertEqual(data["request_id"], "req-123")
        self.assertEqual(data["method"], "GET")
        self.assertEqual(data["path"], "/healthz")
        self.assertEqual(data["status_code"], 200)
        self.assertEqual(data["duration_ms"], 1.5)
        self.assertEqual(data["user"], "tester")

    def test_configure_logging_attaches_json_handler(self) -> None:
        logging_utils = load_logging_utils()
        stream = io.StringIO()
        logger = logging_utils.configure_logging(name="aliecs.test", stream=stream)
        logger.info("hello", extra={"request_id": "r1"})

        line = stream.getvalue().strip().splitlines()[-1]
        data = json.loads(line)
        self.assertEqual(data["message"], "hello")
        self.assertEqual(data["request_id"], "r1")


if __name__ == "__main__":
    unittest.main()


class ValidationErrorLoggingTests(unittest.TestCase):
    """422 要在服务端留下字段路径，但不得把请求体的值写进日志。"""

    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        from fastapi.testclient import TestClient

        from app.main import app

        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = [item for item in sys.path if item != str(BACKEND_ROOT)]

    def _post_invalid(self):
        # 端点先查 token 再校验 body，不给 token 就只会拿到 401，永远走不到 422——
        # 那样两个用例会双双 skip，而 skip 的测试什么也守不住。
        logger = logging.getLogger("aliecs.request")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s|%(fields)s"))
        logger.addHandler(handler)
        try:
            with mock.patch.dict(os.environ, {"GOLD_SPREAD_ALERT_TOKEN": "unit-test-token"}):
                response = self.client.post(
                    "/v1/internal/gold-spread/alerts",
                    json={"event_id": "t", "kind": "not_a_real_kind",
                          "secret_looking_value": "SHOULD-NOT-APPEAR-IN-LOG"},
                    headers={"X-Gold-Spread-Token": "unit-test-token"},
                )
        finally:
            logger.removeHandler(handler)
        return response, stream.getvalue()

    def test_validation_failure_logs_field_paths_but_not_values(self) -> None:
        response, logged = self._post_invalid()
        self.assertEqual(422, response.status_code, "没走到 422 的话这个用例什么都没测")
        self.assertIn("request validation failed", logged)
        self.assertIn("loc", logged)
        self.assertNotIn("SHOULD-NOT-APPEAR-IN-LOG", logged)
        self.assertNotIn("'input'", logged)

    def test_response_body_shape_is_unchanged(self) -> None:
        """调用方按 FastAPI 默认的 {"detail": [...]} 解析，形状不能变。"""
        response, _ = self._post_invalid()
        self.assertEqual(422, response.status_code, "没走到 422 的话这个用例什么都没测")
        body = response.json()
        self.assertIn("detail", body)
        self.assertIsInstance(body["detail"], list)
        self.assertIn("loc", body["detail"][0])
