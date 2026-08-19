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

    def test_sync_routes_are_mounted_and_require_admin(self):
        routes = (
            ("GET", "/v1/sync/overview"),
            ("GET", "/v1/sync/alerts"),
            ("GET", "/v1/sync/runs"),
            ("GET", "/v1/sync/jobs/{job_key}/runs"),
            ("GET", "/v1/sync/runs/{run_id}"),
            ("GET", "/v1/sync/assets"),
            ("GET", "/v1/sync/config/doc"),
            ("PUT", "/v1/sync/config/doc"),
            ("GET", "/v1/sync/config/tplus"),
            ("PUT", "/v1/sync/config/tplus"),
            ("POST", "/v1/sync/run-all"),
            ("POST", "/v1/sync/assets/{source_id}/run"),
            ("POST", "/v1/sync/assets/{source_id}/copy"),
            ("GET", "/v1/sync/assets/{source_id}/download"),
            ("PUT", "/v1/sync/assets/{source_id}/docid"),
            ("GET", "/v1/sync/exports/tplus/{file_name}"),
            ("POST", "/v1/sync/jobs/{job_key}/run"),
        )
        for method, path in routes:
            route = route_for(app, path, method)
            calls = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(require_admin, calls)

    def test_sync_router_registers_only_documented_methods(self):
        methods = {
            method
            for route in sync_router.router.routes
            for method in getattr(route, "methods", set())
        }

        self.assertTrue(methods)
        self.assertTrue(methods <= {"GET", "POST", "PUT"})

    def test_assets_forwards_tplus_catalog_without_leaking_router_state(self):
        expected = {"groups": [{"key": "tplus", "items": []}]}

        with patch.object(sync_router, "_conn"), patch.object(
            sync_router.exports_router, "_latest_tplus_exports", return_value=[]
        ) as latest, patch.object(
            sync_router.sync_control, "assets", return_value=expected
        ) as assets:
            result = sync_router.sync_assets(_={})

        self.assertEqual(expected, result)
        latest.assert_called_once_with()
        assets.assert_called_once_with(unittest.mock.ANY, tplus_items=[])

    def test_config_routes_delegate_to_shared_service(self):
        doc_body = sync_router.sync_control.DocSyncConfigUpdate(
            enabled=True, interval_hours=24, anchor_time="02:00", pull_paused=False
        )
        tplus_body = sync_router.sync_control.SyncConfigUpdate(
            enabled=True, interval_hours=24, anchor_time="01:00"
        )
        with patch.object(sync_router.sync_control, "read_doc_config", return_value={"kind": "doc"}) as read_doc, patch.object(
            sync_router.sync_control, "read_tplus_config", return_value={"kind": "tplus"}
        ) as read_tplus, patch.object(
            sync_router.sync_control, "save_doc_config", return_value={"saved": "doc"}
        ) as save_doc, patch.object(
            sync_router.sync_control, "save_tplus_config", return_value={"saved": "tplus"}
        ) as save_tplus:
            self.assertEqual({"kind": "doc"}, sync_router.sync_doc_config_get(_={}))
            self.assertEqual({"kind": "tplus"}, sync_router.sync_tplus_config_get(_={}))
            self.assertEqual({"saved": "doc"}, sync_router.sync_doc_config_put(doc_body, user={"sub": "admin"}))
            self.assertEqual({"saved": "tplus"}, sync_router.sync_tplus_config_put(tplus_body, user={"sub": "admin"}))

        read_doc.assert_called_once_with(sync_router._conn)
        read_tplus.assert_called_once_with(sync_router._conn)
        save_doc.assert_called_once_with(sync_router._conn, doc_body, "admin")
        save_tplus.assert_called_once_with(sync_router._conn, tplus_body, "admin")

    def test_run_routes_delegate_and_map_invalid_targets_to_400(self):
        with patch.object(sync_router, "_conn") as connect, patch.object(
            sync_router.sync_control, "enqueue_all", return_value={"documents_queued": 2}
        ) as enqueue_all, patch.object(
            sync_router.sync_control, "enqueue_doc_asset", return_value={"queued": True}
        ) as enqueue_asset, patch.object(
            sync_router.sync_control, "enqueue_doc_job", return_value={"queued": True}
        ) as enqueue_job, patch.object(
            sync_router.sync_control, "enqueue_tplus_full", return_value={"queued": True}
        ) as enqueue_tplus:
            self.assertEqual({"documents_queued": 2}, sync_router.sync_run_all(user={"sub": "admin"}))
            self.assertEqual({"queued": True}, sync_router.sync_asset_run(17, user={"sub": "admin"}))
            self.assertEqual({"queued": True}, sync_router.sync_job_run("wecom.doc.17", user={"sub": "admin"}))
            self.assertEqual({"queued": True}, sync_router.sync_job_run("chanjet.full", user={"sub": "admin"}))

        enqueue_all.assert_called_once_with(unittest.mock.ANY, "admin")
        enqueue_asset.assert_called_once_with(unittest.mock.ANY, 17, "admin")
        enqueue_job.assert_called_once_with(unittest.mock.ANY, "wecom.doc.17", "admin")
        enqueue_tplus.assert_called_once_with(unittest.mock.ANY, "admin")
        self.assertEqual(4, connect.call_count)

        with patch.object(sync_router, "_conn"), patch.object(
            sync_router.sync_control,
            "enqueue_doc_asset",
            side_effect=sync_router.sync_control.InvalidSyncTarget("缺少有效企微 docid"),
        ):
            with self.assertRaises(HTTPException) as raised:
                sync_router.sync_asset_run(18, user={"sub": "admin"})
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("缺少有效企微 docid", raised.exception.detail)

    def test_asset_copy_and_docid_repair_delegate_without_echoing_identifiers(self):
        copy_body = sync_router.CopyAssetBody(idempotency_key="action-1")
        repair_body = sync_router.RepairDocIdBody(api_doc_id="dc" + "q" * 86)
        expected_copy = {"status": "registered", "copy_request_id": 3, "source_id": 18}
        expected_repair = {"status": "registered", "source_id": 19, "locator_id": 8}

        with patch.object(sync_router.document_locator, "copy_asset", return_value=expected_copy) as copy_asset, patch.object(
            sync_router.document_locator, "repair_docid", return_value=expected_repair
        ) as repair_docid:
            self.assertEqual(expected_copy, sync_router.sync_asset_copy(17, copy_body, user={"sub": "admin"}))
            self.assertEqual(expected_repair, sync_router.sync_asset_docid(18, repair_body, user={"sub": "admin"}))

        copy_asset.assert_called_once_with(
            sync_router._conn,
            source_id=17,
            idempotency_key="action-1",
            requested_by="admin",
        )
        repair_docid.assert_called_once_with(
            sync_router._conn,
            source_id=18,
            api_doc_id=repair_body.api_doc_id,
            requested_by="admin",
        )
        self.assertNotIn(repair_body.api_doc_id, str(expected_repair))

    def test_canonical_download_routes_delegate_to_legacy_adapters(self):
        external = object()
        tplus = object()
        with patch.object(sync_router.exports_router, "exports_external_doc_download", return_value=external) as doc_download, patch.object(
            sync_router.exports_router, "exports_tplus_download", return_value=tplus
        ) as tplus_download:
            self.assertIs(external, sync_router.sync_asset_download(17, _={}))
            self.assertIs(tplus, sync_router.sync_tplus_download("bom.xlsx", _={}))

        doc_download.assert_called_once_with(17, {})
        tplus_download.assert_called_once_with("bom.xlsx", {})

    def test_asset_action_invalid_target_is_400_and_unexpected_error_is_sanitized(self):
        copy_body = sync_router.CopyAssetBody(idempotency_key="action-2")
        with patch.object(
            sync_router.document_locator,
            "copy_asset",
            side_effect=sync_router.document_locator.InvalidLocatorAction("不支持创建副本"),
        ):
            with self.assertRaises(HTTPException) as invalid:
                sync_router.sync_asset_copy(17, copy_body, user={"sub": "admin"})
        self.assertEqual(400, invalid.exception.status_code)

        repair_body = sync_router.RepairDocIdBody(api_doc_id="dc" + "q" * 86)
        with patch.object(
            sync_router.document_locator,
            "repair_docid",
            side_effect=RuntimeError("password=secret"),
        ):
            with self.assertRaises(HTTPException) as failed:
                sync_router.sync_asset_docid(18, repair_body, user={"sub": "admin"})
        self.assertEqual(500, failed.exception.status_code)
        self.assertNotIn("secret", failed.exception.detail)

    def test_write_route_database_error_is_sanitized(self):
        with patch.object(sync_router, "_conn", side_effect=RuntimeError("password=secret SELECT credentials")):
            with self.assertRaises(HTTPException) as raised:
                sync_router.sync_run_all(user={"sub": "admin"})

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("操作同步中心失败：RuntimeError", raised.exception.detail)
        self.assertNotIn("secret", raised.exception.detail)

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
                group="wecom_company_a",
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
            group="wecom_company_a",
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
