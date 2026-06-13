from __future__ import annotations

import io
import json
import logging
import sys
import unittest
from pathlib import Path


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
