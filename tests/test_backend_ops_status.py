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
        result = self._call_get("/v1/ops/status")

        names = {item["name"] for item in result["hosts"]}
        self.assertIn("AliECS Backend API", names)
        self.assertIn("WebDock API", names)

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
            "diff_json": {"changed": 2},
            "full_snapshot_id": 10,
            "incremental_snapshot_id": 11,
            "created_at": "2026-06-05 10:00:00+08",
            "reviewed_at": None,
            "reviewed_by": None,
            "resolution_json": {},
        }
        main_module._conn = lambda: _FakeOpsConn(self.diff)
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
        self.assertEqual({"changed": 2}, detail.json()["diff_json"])

        action = self.client.post(
            f"/v1/ops/reconciliation/{diff_id}/actions",
            headers={"Authorization": f"Bearer {token}"},
            json={"action": "use_full", "note": "人工确认以全量同步为准"},
        )

        self.assertEqual(200, action.status_code)
        data = action.json()
        self.assertEqual("resolved", data["status"])
        self.assertEqual("use_full", data["resolution"]["action"])


class _FakeOpsConn:
    def __init__(self, diff: dict[str, Any]) -> None:
        self.diff = diff

    def cursor(self) -> "_FakeOpsCursor":
        return _FakeOpsCursor(self.diff)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeOpsCursor:
    def __init__(self, diff: dict[str, Any]) -> None:
        self.diff = diff
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
