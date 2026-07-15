from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_module():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app.routers import miniapp_accounts

    return miniapp_accounts


class FakeCursor:
    def __init__(self, fetchone_script=None, fetchall_script=None):
        self.fetchone_script = list(fetchone_script or [])
        self.fetchall_script = list(fetchall_script or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.fetchone_script.pop(0)

    def fetchall(self):
        return self.fetchall_script.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        pass


class MiniappAccountTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_service_token_is_fail_closed_and_independent(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                self.mod._require_miniapp_service("anything")
        self.assertEqual(ctx.exception.status_code, 503)

        with patch.dict(os.environ, {"MINIAPP_SERVICE_TOKEN": "miniapp-only"}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                self.mod._require_miniapp_service("wrong")
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertIsNone(self.mod._require_miniapp_service("miniapp-only"))

    def test_request_validation_rejects_blank_identity_and_bad_username(self):
        with self.assertRaises(ValidationError):
            self.mod.OpenIdRequest(openid="   ")
        with self.assertRaises(ValidationError):
            self.mod.AccountRequestCreate(
                openid="wx-1", request_type="bind_existing", requested_username="a b", display_name="Alice"
            )

    def test_create_request_stores_openid_mapping_request_only(self):
        cursor = FakeCursor(fetchone_script=[None, None, (11,)])
        conn = FakeConn(cursor)
        body = self.mod.AccountRequestCreate(
            openid="wx-openid-1",
            request_type="create_new",
            requested_username="alice",
            display_name="Alice",
            department="研发",
            reason="配方查询",
        )
        with patch.object(self.mod, "_conn", return_value=conn), patch.object(self.mod, "_audit"):
            result = self.mod.miniapp_create_account_request(body)
        self.assertEqual(result, {"ok": True, "request_id": 11, "status": "pending"})
        self.assertTrue(conn.committed)
        sql = " ".join(item[0] for item in cursor.executed)
        self.assertIn("INSERT INTO miniapp_account_requests", sql)
        self.assertNotIn("INSERT INTO users", sql)
        self.assertNotIn("password", sql.lower())

    def test_status_combines_link_with_existing_rbac(self):
        link = (3, "active", 7, "alice", "Alice", "active", False)
        latest = (11, "bind_existing", "alice", "Alice", None, None, "approved", 7, None, "created", "reviewed")
        cursor = FakeCursor(fetchone_script=[link, latest])
        with patch.object(self.mod, "_conn", return_value=FakeConn(cursor)), patch.object(
            self.mod, "_user_roles_permissions", return_value=(["formula_user"], ["formula.read"])
        ):
            result = self.mod._account_status("wx-openid-1")
        self.assertTrue(result["linked"])
        self.assertTrue(result["authorized"])
        self.assertEqual(result["account"]["username"], "alice")
        self.assertEqual(result["request"]["status"], "approved")

    def test_approve_links_existing_user_without_creating_sso_credentials(self):
        cursor = FakeCursor(fetchone_script=[("wx-openid-1", "pending"), (7, "active"), None])
        conn = FakeConn(cursor)
        body = self.mod.AccountRequestReview(user_id=7, review_note="已核实")
        actor = {"uid": 1, "username": "admin", "roles": ["admin"]}
        with patch.object(self.mod, "_conn", return_value=conn), patch.object(self.mod, "_audit"):
            result = self.mod.admin_approve_miniapp_account_request(11, body, actor)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["user_id"], 7)
        self.assertTrue(conn.committed)
        sql = " ".join(item[0] for item in cursor.executed)
        self.assertIn("INSERT INTO miniapp_account_links", sql)
        self.assertNotIn("INSERT INTO users", sql)
        self.assertNotIn("password", sql.lower())

    def test_approve_requires_preprovisioned_user(self):
        body = self.mod.AccountRequestReview(review_note="")
        with self.assertRaises(HTTPException) as ctx:
            self.mod.admin_approve_miniapp_account_request(11, body, {"uid": 1, "username": "admin"})
        self.assertEqual(ctx.exception.status_code, 422)

    def test_migration_enforces_one_to_one_links_and_pending_request(self):
        sql = (Path(__file__).resolve().parents[1] / "db" / "migrations" / "0029_miniapp_account_linking.sql").read_text(encoding="utf-8")
        self.assertIn("openid TEXT NOT NULL UNIQUE", sql)
        self.assertIn("user_id BIGINT NOT NULL UNIQUE", sql)
        self.assertIn("WHERE status = 'pending'", sql)
        self.assertNotIn("password", sql.lower())


if __name__ == "__main__":
    unittest.main()
