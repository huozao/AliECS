from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_auth_admin():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app.routers import auth_admin

    return auth_admin


class LocalLoginGateTests(unittest.TestCase):
    def test_login_disabled_returns_403(self) -> None:
        mod = load_auth_admin()
        with patch.dict(os.environ, {"LOCAL_LOGIN_ENABLED": "false"}, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                mod.auth_login(mod.LoginRequest(username="admin", password="x"))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_register_disabled_returns_403(self) -> None:
        mod = load_auth_admin()
        with patch.dict(os.environ, {"LOCAL_LOGIN_ENABLED": "false"}, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                mod.auth_register(mod.RegisterRequest(username="bob", password="x"))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_default_missing_env_keeps_login_enabled(self) -> None:
        # Fail-safe: a missing env must NOT lock everyone out; the gate only
        # closes when explicitly disabled. Prove we get past the gate into the
        # (mocked) bootstrap/DB path rather than a 403.
        mod = load_auth_admin()
        sentinel = RuntimeError("reached-db-path")
        env = {k: v for k, v in os.environ.items() if k != "LOCAL_LOGIN_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(mod, "_bootstrap_admin_if_needed", side_effect=sentinel):
                with self.assertRaises(RuntimeError) as ctx:
                    mod.auth_login(mod.LoginRequest(username="admin", password="x"))
        self.assertEqual(str(ctx.exception), "reached-db-path")

    def test_explicit_true_keeps_login_enabled(self) -> None:
        mod = load_auth_admin()
        sentinel = RuntimeError("reached-db-path")
        with patch.dict(os.environ, {"LOCAL_LOGIN_ENABLED": "true"}, clear=False):
            with patch.object(mod, "_bootstrap_admin_if_needed", side_effect=sentinel):
                with self.assertRaises(RuntimeError):
                    mod.auth_login(mod.LoginRequest(username="admin", password="x"))


if __name__ == "__main__":
    unittest.main()
