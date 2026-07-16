from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class RoutingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        from app.routers import auth_admin as admin_module
        from app.routers import exports as main_module

        cls.main = main_module
        cls.admin = admin_module

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]

    def test_wechat_routing_api_only_returns_enabled_project_lanes(self) -> None:
        rows = [
            ("wxid_a", "张三", "https://chatgpt.com/g/p1/project", "项目一"),
            ("wxid_b", "李四", "", "无项目"),
        ]
        old_conn = self.main._conn
        self.main._conn = lambda: FakeConn(rows)
        try:
            body = self.main.routing_wechat_projects()
        finally:
            self.main._conn = old_conn

        self.assertEqual(
            {"lanes": {"wxid_a": {"name": "张三", "project_url": "https://chatgpt.com/g/p1/project"}}},
            body,
        )

    def test_feishu_routing_api_uses_feishu_channel(self) -> None:
        rows = [("ou_x", "李四", "https://chatgpt.com/g/f1/project", "飞书项目")]
        old_conn = self.main._conn
        self.main._conn = lambda: FakeConn(rows)
        try:
            body = self.main.routing_feishu_projects()
        finally:
            self.main._conn = old_conn

        self.assertEqual("https://chatgpt.com/g/f1/project", body["lanes"]["ou_x"]["project_url"])

    def test_wecom_routing_api_uses_wecom_channel(self) -> None:
        rows = [("wr_group", "研发群", "https://chatgpt.com/g/q1/project", "企微研发")]
        old_conn = self.main._conn
        self.main._conn = lambda: FakeConn(rows)
        try:
            body = self.main.routing_wecom_projects()
        finally:
            self.main._conn = old_conn

        self.assertEqual("https://chatgpt.com/g/q1/project", body["lanes"]["wr_group"]["project_url"])

    def test_admin_contacts_can_upsert_contact(self) -> None:
        conn = FakeAdminConn()
        old_conn = self.admin._conn
        old_audit = self.admin._audit
        self.admin._conn = lambda: conn
        self.admin._audit = lambda *args, **kwargs: None
        try:
            body = self.admin.ManagedContactUpsertRequest(
                channel="wechat",
                peer_id="wxid_a",
                display_name="张三",
                enabled=False,
                project_url="https://chatgpt.com/g/p2/project",
            )
            self.admin.admin_upsert_contact(body, actor={"sub": "admin"})
        finally:
            self.admin._conn = old_conn
            self.admin._audit = old_audit

        self.assertEqual("wechat", conn.params[0])
        self.assertEqual("wxid_a", conn.params[1])
        self.assertFalse(conn.params[4])
        self.assertEqual("https://chatgpt.com/g/p2/project", conn.params[5])
        self.assertTrue(conn.committed)


class FakeConn:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)

    def close(self) -> None:
        return None


class FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


class FakeAdminConn:
    def __init__(self) -> None:
        self.params = None
        self.committed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self.sql = sql
        self.params = params

    def fetchone(self):
        return (123,)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
