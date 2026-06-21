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
        self._old_hosts = os.environ.get("OPS_HEALTH_HTTP_TARGETS_JSON")
        os.environ.pop("DATABASE_URL", None)

    def tearDown(self) -> None:
        if self._old_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._old_database_url
        if self._old_hosts is None:
            os.environ.pop("OPS_HEALTH_HTTP_TARGETS_JSON", None)
        else:
            os.environ["OPS_HEALTH_HTTP_TARGETS_JSON"] = self._old_hosts

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

    def test_ops_status_uses_default_external_hosts_when_env_is_empty(self) -> None:
        # WebDock API is always present (defaults to the in-host SSH tunnel),
        # but the public-facing Backend/Public-Web rows only appear once the
        # tenant configures OPS_HEALTH_BACKEND_URL / OPS_HEALTH_PUBLIC_WEB_URL.
        result = self._call_get("/v1/ops/status")

        names = {item["name"] for item in result["hosts"]}
        self.assertIn("WebDock API", names)
        self.assertNotIn("AliECS Backend API", names)
        self.assertNotIn("AliECS Public Web", names)

    def test_ops_status_shows_backend_row_when_env_configured(self) -> None:
        os.environ["OPS_HEALTH_BACKEND_URL"] = "https://example.com/api/healthz"
        os.environ["OPS_HEALTH_PUBLIC_WEB_URL"] = "https://example.com/"
        try:
            result = self._call_get("/v1/ops/status")
        finally:
            os.environ.pop("OPS_HEALTH_BACKEND_URL", None)
            os.environ.pop("OPS_HEALTH_PUBLIC_WEB_URL", None)

        names = {item["name"] for item in result["hosts"]}
        self.assertIn("AliECS Backend API", names)
        self.assertIn("AliECS Public Web", names)

    def test_default_webdock_host_uses_ecs_ssh_tunnel_not_tailscale(self) -> None:
        from app.main import _ops_http_targets

        targets = _ops_http_targets()
        webdock_api = next(item for item in targets if item["name"] == "WebDock API")

        self.assertIn("11800", webdock_api["url"])
        self.assertNotIn("100.97.", webdock_api["url"])
        self.assertIn("SSH", webdock_api["description"])

    def test_ops_host_detail_can_refresh_configured_target(self) -> None:
        import json
        from fastapi.testclient import TestClient

        os.environ["OPS_HEALTH_HTTP_TARGETS_JSON"] = json.dumps(
            [{"name": "Missing Test Target", "url": "http://127.0.0.1:9", "timeout": 0.1}]
        )
        client = TestClient(self.app)

        response = client.get("/v1/ops/hosts/Missing%20Test%20Target/refresh")

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual("Missing Test Target", data["name"])
        self.assertFalse(data["ok"])
        self.assertIn("last_checked_at", data)

    def test_default_features_put_inventory_and_system_formula_first(self) -> None:
        from app.main import DEFAULT_FEATURES

        active = [item for item in sorted(DEFAULT_FEATURES, key=lambda x: x["sort_order"]) if item["status"] == "active"]

        self.assertEqual(["raw_inventory", "finished_inventory", "formula_query"], [item["code"] for item in active[:3]])
        self.assertEqual("系统配方", active[2]["title"])
        self.assertEqual("/formula/", active[2]["url"])
        self.assertEqual("formula.read", active[2]["required_permission"])

    def test_tplus_recent_requests_include_detail_payload_for_manual_check(self) -> None:
        from app import main as main_module

        old_conn = main_module._conn
        main_module._conn = lambda: _FakeTplusStatusConn()
        try:
            status = main_module._tplus_status_from_db()
        finally:
            main_module._conn = old_conn

        request = status["recent_requests"][0]
        self.assertEqual({"code": "6830"}, request["target_json"])
        self.assertEqual({"mode": "incremental", "target_json": {"code": "6830"}}, request["detail_json"])
        self.assertEqual({}, request["error_json"])
        self.assertEqual(501, request["row_count"])
        self.assertEqual(88, request["sync_run_id"])

    def test_tplus_runs_include_request_context_for_origin_labels(self) -> None:
        from app import main as main_module

        old_conn = main_module._conn
        main_module._conn = lambda: _FakeTplusRunsConn()
        try:
            result = main_module.ops_tplus_runs(limit=20, offset=0, _={})
        finally:
            main_module._conn = old_conn

        run = result["items"][0]
        self.assertEqual(58, run["request_id"])
        self.assertEqual("10728331-569a-443f-89ad-b8b22df7a591", run["reason_event_id"])

    def test_formula_cost_rbac_seed_includes_requested_roles_and_permission(self) -> None:
        migration = Path(__file__).resolve().parents[1] / "db" / "migrations" / "0012_formula_cost_rbac.sql"
        sql = migration.read_text(encoding="utf-8")

        for role_code in [
            "chairman",
            "general_manager_a",
            "general_manager_b",
            "sales_a",
            "sales_b",
            "tech_a",
            "tech_b",
            "finance_a",
            "finance_b",
            "warehouse_a",
            "warehouse_b",
        ]:
            self.assertIn(f"'{role_code}'", sql)

        self.assertIn("'formula.cost.calculate'", sql)
        self.assertIn("'配方成本核算'", sql)
        self.assertIn("WHERE r.code = 'admin' AND p.code = 'formula.cost.calculate'", sql)


class _FakeTplusStatusConn:
    def cursor(self) -> "_FakeTplusStatusCursor":
        return _FakeTplusStatusCursor()

    def close(self) -> None:
        pass


class _FakeTplusStatusCursor:
    def __init__(self) -> None:
        self._rows: list[tuple[Any, ...]] = []
        self._one: tuple[Any, ...] | None = None

    def __enter__(self) -> "_FakeTplusStatusCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> None:
        normalized = " ".join(sql.lower().split())
        self._rows = []
        self._one = None
        if normalized.startswith("select status, count(*)"):
            self._rows = [("pending", 0), ("failed", 0)]
            return
        if normalized.startswith("select id, module, mode, status, started_at, finished_at, row_count"):
            self._one = (88, "bom", "incremental", "success", "2026-06-15 15:55:32+08", "2026-06-15 15:56:13+08", 501, 0, {"run": 1})
            return
        if normalized.startswith("select finished_at"):
            self._one = ("2026-06-15 15:56:13+08",)
            return
        if normalized.startswith("select r.id, r.module, r.mode, r.status"):
            self._rows = [
                (
                    58,
                    "bom",
                    "incremental",
                    "success",
                    "2026-06-15 15:55:32+08",
                    "2026-06-15 15:55:33+08",
                    "2026-06-15 15:56:13+08",
                    "10728331-569a-443f-89ad-b8b22df7a591",
                    {"code": "6830"},
                    88,
                    {},
                    {"mode": "incremental", "target_json": {"code": "6830"}},
                    {},
                    501,
                    0,
                )
            ]
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one


class _FakeTplusRunsConn:
    def cursor(self) -> "_FakeTplusRunsCursor":
        return _FakeTplusRunsCursor()

    def close(self) -> None:
        pass


class _FakeTplusRunsCursor:
    def __init__(self) -> None:
        self._rows: list[tuple[Any, ...]] = []
        self._one: tuple[Any, ...] | None = None

    def __enter__(self) -> "_FakeTplusRunsCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> None:
        normalized = " ".join(sql.lower().split())
        self._rows = []
        self._one = None
        if normalized.startswith("select count(*) from integration_sync_runs"):
            self._one = (1,)
            return
        if "from integration_sync_runs" in normalized:
            self._rows = [
                (
                    88,
                    "bom",
                    "incremental",
                    "success",
                    "2026-06-15 15:56:13+08",
                    0,
                    501,
                    58,
                    "10728331-569a-443f-89ad-b8b22df7a591",
                )
            ]
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one


class BackendOpsDatabaseActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        self._old_env = {
            "AUTH_TOKEN_SECRET": os.environ.get("AUTH_TOKEN_SECRET"),
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
        }
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]
        sys.path.insert(0, backend_root)
        os.environ["AUTH_TOKEN_SECRET"] = "test-ops-secret"
        os.environ["DATABASE_URL"] = "postgresql://unit-test/not-used"

        from fastapi.testclient import TestClient
        from app import main as main_module
        from app.main import _encode_token, app

        self.diff = {
            "id": 1,
            "provider": "chanjet",
            "module": "bom",
            "status": "needs_review",
            "severity": "warning",
            "summary": "BOM snapshot differs",
            "diff_json": {"changed": 2, "current_snapshot_id": 11, "previous_snapshot_id": 10},
            "full_snapshot_id": 10,
            "incremental_snapshot_id": 11,
            "created_at": "2026-06-05 10:00:00+08",
            "reviewed_at": None,
            "reviewed_by": None,
            "resolution_json": {},
        }
        self.fake_conn = _FakeOpsConn(self.diff)
        main_module._conn = lambda: self.fake_conn
        self.main_module = main_module
        self._encode_token = _encode_token
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = self._old_sys_path

    def _token(self, *, roles: list[str] | None = None, permissions: list[str] | None = None) -> str:
        import time

        return self._encode_token(
            {
                "sub": "ops-admin",
                "roles": roles or [],
                "permissions": permissions or [],
                "exp": int(time.time()) + 3600,
            }
        )

    def test_admin_can_fetch_reconciliation_detail_and_mark_resolution(self) -> None:
        diff_id = self.diff["id"]
        token = self._token(roles=["admin"], permissions=["admin.access"])

        detail = self.client.get(
            f"/v1/ops/reconciliation/{diff_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(200, detail.status_code)
        self.assertEqual(2, detail.json()["diff_json"]["changed"])

        from unittest.mock import patch

        with patch.object(
            self.main_module,
            "_activate_bom_snapshot",
            return_value={"active_export_name": "bom_20260606_061353.xlsx", "selected_snapshot_id": 10},
        ):
            action = self.client.post(
                f"/v1/ops/reconciliation/{diff_id}/actions",
                headers={"Authorization": f"Bearer {token}"},
                json={"action": "use_full", "note": "人工确认以全量同步为准"},
            )

        self.assertEqual(200, action.status_code)
        data = action.json()
        self.assertEqual("resolved", data["status"])
        self.assertEqual("use_full", data["resolution"]["action"])

    def test_admin_resolution_activates_selected_bom_file_and_supersedes_older_diffs(self) -> None:
        diff_id = self.diff["id"]
        token = self._token(roles=["admin"], permissions=["admin.access"])

        from unittest.mock import patch

        with patch.object(
            self.main_module,
            "_activate_bom_snapshot",
            return_value={
                "active_export_name": "bom_20260606_061353.xlsx",
                "active_export_source": "snapshot_records",
                "selected_snapshot_id": 11,
            },
        ) as activate:
            response = self.client.post(
                f"/v1/ops/reconciliation/{diff_id}/actions",
                headers={"Authorization": f"Bearer {token}"},
                json={"action": "use_current", "note": "采用当前变动快照"},
            )

        self.assertEqual(200, response.status_code)
        data = response.json()
        activate.assert_called_once()
        self.assertEqual("resolved", data["status"])
        self.assertEqual("use_current", data["resolution"]["action"])
        self.assertEqual(11, data["resolution"]["selected_snapshot_id"])
        self.assertEqual("bom_20260606_061353.xlsx", data["resolution"]["active_export_name"])
        self.assertEqual([0], self.fake_conn.superseded_diff_ids)


class _FakeOpsConn:
    def __init__(self, diff: dict[str, Any]) -> None:
        self.diff = diff
        self.superseded_diff_ids: list[int] = []

    def cursor(self) -> "_FakeOpsCursor":
        return _FakeOpsCursor(self)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeOpsCursor:
    def __init__(self, conn: _FakeOpsConn) -> None:
        self.conn = conn
        self.diff = conn.diff
        self._one: tuple[Any, ...] | None = None

    def __enter__(self) -> "_FakeOpsCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> None:
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("select id, provider, module, status"):
            self._one = self._row()
            return
        if "update integration_reconciliation_diffs" in normalized and "where provider = %s" in normalized:
            self.conn.superseded_diff_ids.append(0)
            self._one = None
            return
        if normalized.startswith("update integration_reconciliation_diffs"):
            action_status, resolution_json, reviewed_by, diff_id = params or []
            if int(diff_id) != self.diff["id"]:
                self._one = None
                return
            self.diff["status"] = action_status
            self.diff["resolution_json"] = resolution_json
            self.diff["reviewed_by"] = reviewed_by
            self.diff["reviewed_at"] = "2026-06-05 10:05:00+08"
            self._one = self._row()
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def _row(self) -> tuple[Any, ...]:
        return (
            self.diff["id"],
            self.diff["provider"],
            self.diff["module"],
            self.diff["status"],
            self.diff["severity"],
            self.diff["summary"],
            self.diff["diff_json"],
            self.diff["full_snapshot_id"],
            self.diff["incremental_snapshot_id"],
            self.diff["created_at"],
            self.diff["reviewed_at"],
            self.diff["reviewed_by"],
            self.diff["resolution_json"],
        )


if __name__ == "__main__":
    unittest.main()
