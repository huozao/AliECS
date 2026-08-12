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
        for path in (
            "/v1/sync/overview",
            "/v1/sync/alerts",
            "/v1/sync/runs",
            "/v1/sync/jobs/{job_key}/runs",
            "/v1/sync/runs/{run_id}",
        ):
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

    def test_overview_preserves_classified_http_exception(self):
        expected = HTTPException(status_code=409, detail="classified overview")

        with patch.object(sync_router, "_conn"), patch.object(
            sync_router.sync_read, "overview", side_effect=expected
        ):
            with self.assertRaises(HTTPException) as raised:
                sync_router.sync_overview(_={})

        self.assertIs(expected, raised.exception)
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("classified overview", raised.exception.detail)

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

    def test_alert_preserves_classified_http_exception(self):
        expected = HTTPException(status_code=404, detail="classified alert")

        with patch.object(sync_router, "_conn"), patch.object(
            sync_router.sync_read, "alerts_page", side_effect=expected
        ):
            with self.assertRaises(HTTPException) as raised:
                sync_router.sync_alerts(
                    state="open", limit=50, offset=0, _={}
                )

        self.assertIs(expected, raised.exception)
        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual("classified alert", raised.exception.detail)

    def test_invalid_run_status_returns_422_before_database_access(self):
        isolated = FastAPI()
        isolated.include_router(sync_router.router)
        isolated.dependency_overrides[require_admin] = lambda: {"roles": ["admin"]}

        with TestClient(isolated) as client, patch.object(
            sync_router, "_conn", side_effect=AssertionError("database must not be used")
        ):
            global_response = client.get("/v1/sync/runs?status=invalid")
            job_response = client.get(
                "/v1/sync/jobs/wecom.doc.17/runs?status=invalid"
            )

        self.assertEqual(422, global_response.status_code)
        self.assertEqual(422, job_response.status_code)

    def test_global_runs_forwards_filters_to_shared_read(self):
        expected = {"items": [], "total": 0, "limit": 20, "offset": 40}

        with patch.object(sync_router, "_conn"), patch.object(
            sync_router.sync_read, "runs_page", return_value=expected
        ) as runs_page:
            result = sync_router.sync_runs(
                job_key="wecom.doc.17",
                provider="wecom",
                status="failed",
                limit=20,
                offset=40,
                _={},
            )

        self.assertEqual(expected, result)
        runs_page.assert_called_once_with(
            unittest.mock.ANY,
            job_key="wecom.doc.17",
            provider="wecom",
            status="failed",
            limit=20,
            offset=40,
        )

    def test_missing_job_and_run_return_404(self):
        with patch.object(sync_router, "_conn"), patch.object(
            sync_router.sync_read, "job_exists", return_value=False
        ):
            with self.assertRaises(HTTPException) as job_error:
                sync_router.sync_job_runs(
                    job_key="missing", status=None, limit=20, offset=0, _={}
                )

        self.assertEqual(404, job_error.exception.status_code)
        self.assertEqual("sync job not found", job_error.exception.detail)

        with patch.object(sync_router, "_conn"), patch.object(
            sync_router.sync_read, "run_detail", return_value=None
        ):
            with self.assertRaises(HTTPException) as run_error:
                sync_router.sync_run_detail(run_id=404, _={})

        self.assertEqual(404, run_error.exception.status_code)
        self.assertEqual("sync run not found", run_error.exception.detail)

    def test_job_runs_checks_existence_before_querying_page(self):
        expected = {"items": [], "total": 0, "limit": 20, "offset": 0}

        with patch.object(sync_router, "_conn"), patch.object(
            sync_router.sync_read, "job_exists", return_value=True
        ) as job_exists, patch.object(
            sync_router.sync_read, "runs_page", return_value=expected
        ) as runs_page:
            result = sync_router.sync_job_runs(
                job_key="wecom.doc.17", status="success", limit=20, offset=0, _={}
            )

        self.assertEqual(expected, result)
        connection = job_exists.call_args.args[0]
        job_exists.assert_called_once_with(connection, "wecom.doc.17")
        runs_page.assert_called_once_with(
            connection,
            job_key="wecom.doc.17",
            provider=None,
            status="success",
            limit=20,
            offset=0,
        )

    def test_run_database_error_exposes_only_exception_type(self):
        raw_message = "postgresql://secret@db SELECT * FROM credentials"

        with patch.object(sync_router, "_conn", side_effect=RuntimeError(raw_message)):
            with self.assertRaises(HTTPException) as raised:
                sync_router.sync_runs(
                    job_key=None,
                    provider=None,
                    status=None,
                    limit=20,
                    offset=0,
                    _={},
                )

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("读取同步中心失败：RuntimeError", raised.exception.detail)
        self.assertNotIn("secret", raised.exception.detail)
        self.assertNotIn("SELECT", raised.exception.detail)

    def test_run_route_preserves_classified_http_exception(self):
        expected = HTTPException(status_code=409, detail="classified run")

        with patch.object(sync_router, "_conn"), patch.object(
            sync_router.sync_read, "runs_page", side_effect=expected
        ):
            with self.assertRaises(HTTPException) as raised:
                sync_router.sync_runs(
                    job_key=None,
                    provider=None,
                    status=None,
                    limit=20,
                    offset=0,
                    _={},
                )

        self.assertIs(expected, raised.exception)


if __name__ == "__main__":
    unittest.main()
