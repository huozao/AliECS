from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))

from app.core import require_admin
from app.main import app

try:
    from app.routers import sync as sync_router
except ImportError:
    sync_router = None


def route_for(target: FastAPI, path: str, method: str):
    for route in target.routes:
        if getattr(route, "path", None) == path and method in getattr(
            route, "methods", set()
        ):
            return route
    raise AssertionError(f"missing route: {method} {path}")


class SyncApiTests(unittest.TestCase):
    def setUp(self) -> None:
        if sync_router is None:
            self.fail("app.routers.sync is not implemented")

    def test_sync_get_routes_are_mounted_and_require_admin(self):
        for path in ("/v1/sync/overview", "/v1/sync/alerts"):
            route = route_for(app, path, "GET")
            calls = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(require_admin, calls)

    def test_sync_router_registers_no_write_methods(self):
        methods = {
            method
            for route in sync_router.router.routes
            for method in getattr(route, "methods", set())
        }

        self.assertTrue(methods)
        self.assertTrue(methods <= {"GET"})

    def test_invalid_alert_state_returns_422_before_database_access(self):
        isolated = FastAPI()
        isolated.include_router(sync_router.router)
        isolated.dependency_overrides[require_admin] = lambda: {"roles": ["admin"]}

        with TestClient(isolated) as client, patch.object(
            sync_router, "_conn", side_effect=AssertionError("database must not be used")
        ):
            response = client.get("/v1/sync/alerts?state=invalid")

        self.assertEqual(422, response.status_code)

    def test_overview_database_error_exposes_only_exception_type(self):
        raw_message = "postgresql://secret@db SELECT * FROM credentials"

        with patch.object(sync_router, "_conn", side_effect=RuntimeError(raw_message)):
            with self.assertRaises(HTTPException) as raised:
                sync_router.sync_overview(_={})

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual(
            "读取同步中心失败：RuntimeError", raised.exception.detail
        )
        self.assertNotIn("secret", raised.exception.detail)
        self.assertNotIn("SELECT", raised.exception.detail)

    def test_alert_database_error_exposes_only_exception_type(self):
        raw_message = "postgresql://secret@db SELECT * FROM credentials"

        with patch.object(sync_router, "_conn", side_effect=ValueError(raw_message)):
            with self.assertRaises(HTTPException) as raised:
                sync_router.sync_alerts(
                    state="open", limit=50, offset=0, _={}
                )

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual(
            "读取同步中心失败：ValueError", raised.exception.detail
        )
        self.assertNotIn("secret", raised.exception.detail)
        self.assertNotIn("SELECT", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
