from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"

OIDC_ENV = {
    "ENV": "dev",
    "AUTH_TOKEN_SECRET": "x" * 32,
    "OIDC_ENABLED": "true",
    "OIDC_ISSUER": "https://auth.hydwang.xyz",
    "OIDC_CLIENT_ID": "website",
    "OIDC_CLIENT_SECRET": "client-secret",
    "OIDC_REDIRECT_URI": "https://hydwang.xyz/api/v1/auth/oidc/callback",
}

DISCOVERY = {
    "authorization_endpoint": "https://auth.hydwang.xyz/api/oidc/authorization",
    "token_endpoint": "https://auth.hydwang.xyz/api/oidc/token",
    "userinfo_endpoint": "https://auth.hydwang.xyz/api/oidc/userinfo",
}

USERINFO = {"sub": "sub-123", "preferred_username": "alice", "groups": ["website_users"]}

# users 行序: id, username, display_name, status, is_admin, token_version
ALICE = (1, "alice", "Alice", "active", False, 1)


def load_oidc():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app.routers import auth_oidc

    return auth_oidc


class FakeCursor:
    def __init__(self, fetchone_script):
        self.fetchone_script = list(fetchone_script)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.fetchone_script.pop(0)

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

    def rollback(self):
        pass

    def close(self):
        pass


class OidcLoginTests(unittest.TestCase):
    def test_login_404_when_disabled(self):
        mod = load_oidc()
        with patch.dict(os.environ, {**OIDC_ENV, "OIDC_ENABLED": "false"}, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                mod.oidc_login()
        self.assertEqual(ctx.exception.status_code, 404)

    def test_login_redirects_with_pkce_and_state(self):
        mod = load_oidc()
        with patch.dict(os.environ, OIDC_ENV, clear=False):
            with patch.object(mod, "_http_get_json", return_value=DISCOVERY):
                response = mod.oidc_login()
        self.assertEqual(response.status_code, 302)
        location = response.headers["location"]
        self.assertTrue(location.startswith(DISCOVERY["authorization_endpoint"]))
        self.assertIn("code_challenge_method=S256", location)
        self.assertIn("client_id=website", location)
        self.assertEqual(len(mod._pending_states), 1)

    def test_callback_rejects_unknown_state(self):
        mod = load_oidc()
        with patch.dict(os.environ, OIDC_ENV, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                mod.oidc_callback(code="c", state="nope")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_callback_first_login_binds_by_username_and_sets_token(self):
        mod = load_oidc()
        cursor = FakeCursor([None, ALICE])  # sub 未命中 -> username 绑定 RETURNING 命中
        conn = FakeConn(cursor)
        mod._pending_states["s1"] = ("verifier-1", time.time())
        with patch.dict(os.environ, OIDC_ENV, clear=False):
            with patch.object(mod, "_http_get_json", side_effect=[DISCOVERY, USERINFO]):
                with patch.object(mod, "_http_post_form", return_value={"access_token": "at-1"}) as post_form:
                    with patch.object(mod, "_conn", return_value=conn):
                        with patch.object(mod, "_audit") as audit:
                            with patch.object(mod, "_user_roles_permissions", return_value=([], [])):
                                response = mod.oidc_callback(code="code-1", state="s1")
        body = response.body.decode("utf-8")
        self.assertIn("aliecs_auth_token", body)
        self.assertTrue(conn.committed)
        self.assertEqual(post_form.call_args.args[1]["code_verifier"], "verifier-1")
        bind_sql, bind_params = cursor.executed[1]
        self.assertIn("SET oidc_sub", bind_sql)
        self.assertEqual(bind_params, ("sub-123", "alice"))
        audit.assert_called_once_with("alice", "auth.oidc.login")

    def test_callback_unknown_user_403(self):
        mod = load_oidc()
        cursor = FakeCursor([None, None])
        conn = FakeConn(cursor)
        mod._pending_states["s2"] = ("verifier-2", time.time())
        with patch.dict(os.environ, OIDC_ENV, clear=False):
            with patch.object(mod, "_http_get_json", side_effect=[DISCOVERY, USERINFO]):
                with patch.object(mod, "_http_post_form", return_value={"access_token": "at-2"}):
                    with patch.object(mod, "_conn", return_value=conn):
                        with self.assertRaises(HTTPException) as ctx:
                            mod.oidc_callback(code="code-2", state="s2")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
