from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class ClashProfileEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        self._old_env = {k: os.environ.get(k) for k in ("AUTH_TOKEN_SECRET", "CLASH_SELF_NODES_JSON")}
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]
        sys.path.insert(0, backend_root)
        os.environ["AUTH_TOKEN_SECRET"] = "test-clash-profile-secret"
        from app.routers import clash_profile

        self.module = clash_profile
        from fastapi import HTTPException

        self.HTTPException = HTTPException

    def tearDown(self) -> None:
        sys.path[:] = self._old_sys_path
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]

    def test_missing_env_raises_500(self) -> None:
        os.environ.pop("CLASH_SELF_NODES_JSON", None)
        with self.assertRaises(self.HTTPException) as ctx:
            self.module._load_self_nodes()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_invalid_json_raises_500(self) -> None:
        os.environ["CLASH_SELF_NODES_JSON"] = "{not json"
        with self.assertRaises(self.HTTPException) as ctx:
            self.module._load_self_nodes()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_non_list_raises_500(self) -> None:
        os.environ["CLASH_SELF_NODES_JSON"] = '{"name": "x"}'
        with self.assertRaises(self.HTTPException) as ctx:
            self.module._load_self_nodes()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_empty_list_raises_500(self) -> None:
        os.environ["CLASH_SELF_NODES_JSON"] = "[]"
        with self.assertRaises(self.HTTPException) as ctx:
            self.module._load_self_nodes()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_node_without_required_keys_raises_500(self) -> None:
        os.environ["CLASH_SELF_NODES_JSON"] = '[{"name": "a"}]'
        with self.assertRaises(self.HTTPException) as ctx:
            self.module._load_self_nodes()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_valid_env_returns_nodes(self) -> None:
        os.environ["CLASH_SELF_NODES_JSON"] = '[{"name": "a", "server": "203.0.113.10", "type": "vless"}]'
        nodes = self.module._load_self_nodes()
        self.assertEqual(nodes[0]["server"], "203.0.113.10")

    def test_router_prefix_is_admin_scoped(self) -> None:
        self.assertEqual(self.module.router.prefix, "/v1/admin/clash-profile")


if __name__ == "__main__":
    unittest.main()
