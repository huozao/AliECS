from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class BackendOpsStatusTests(unittest.TestCase):
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
        self._old_database_url = os.environ.get("DATABASE_URL")
        os.environ.pop("DATABASE_URL", None)

    def tearDown(self) -> None:
        if self._old_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._old_database_url

    def _call_get(self, path: str) -> dict[str, Any]:
        for route in self.app.routes:
            if getattr(route, "path", "") == path and "GET" in getattr(route, "methods", set()):
                return route.endpoint()
        self.fail(f"missing GET route: {path}")

    def test_ops_status_returns_attention_ready_shape_without_database(self) -> None:
        result = self._call_get("/v1/ops/status")

        self.assertEqual("degraded", result["status"])
        self.assertFalse(result["database"]["ok"])
        self.assertIn("system", result)
        self.assertIn("tplus", result)
        self.assertIn("reconciliation", result)
        self.assertIn("hosts", result)
        self.assertIn("attention_items", result)
        self.assertIn("database_unhealthy", [item["code"] for item in result["attention_items"]])


if __name__ == "__main__":
    unittest.main()
