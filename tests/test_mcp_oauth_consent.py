from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

from mcp.server.auth.provider import AuthorizationParams
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
SVC = ROOT / "services" / "mcp-coding-server" / "app"
_PKG = "mcp_oauth_pkg"


def load_oauth():
    spec = importlib.util.spec_from_file_location(
        _PKG, SVC / "__init__.py", submodule_search_locations=[str(SVC)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = pkg
    spec.loader.exec_module(pkg)
    sub = importlib.util.spec_from_file_location(
        f"{_PKG}.oauth",
        SVC / "oauth" / "__init__.py",
        submodule_search_locations=[str(SVC / "oauth")],
    )
    oauth = importlib.util.module_from_spec(sub)
    sys.modules[f"{_PKG}.oauth"] = oauth
    sub.loader.exec_module(oauth)
    mods = {}
    for name in ("config", "store", "provider", "consent"):
        s = importlib.util.spec_from_file_location(
            f"{_PKG}.oauth.{name}", SVC / "oauth" / f"{name}.py"
        )
        m = importlib.util.module_from_spec(s)
        sys.modules[f"{_PKG}.oauth.{name}"] = m
        s.loader.exec_module(m)
        mods[name] = m
    return mods


def _run(coro):
    return asyncio.run(coro)


def _get_request(path, query=b""):
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query,
        "headers": [],
    }
    return Request(scope)


def _post_request(form: dict):
    body = "&".join(f"{k}={v}" for k, v in form.items()).encode()
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/oauth/consent",
        "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }
    return Request(scope, receive)


class ConsentTests(unittest.TestCase):
    def _setup(self):
        m = load_oauth()
        cfg = m["config"].OAuthConfig(
            enabled=True,
            issuer_url="https://h.xyz/mcp-x",
            passphrase="hunter2",
            pepper="p" * 32,
            store_path=":memory:",
            code_ttl=60,
        )
        store = m["store"].OAuthStore(":memory:", cfg.pepper)
        prov = m["provider"].AliecsOAuthProvider(cfg, store)
        handler = m["consent"].make_consent_handler(prov, cfg)
        params = AuthorizationParams(
            state="st",
            scopes=["coding"],
            code_challenge="chal",
            redirect_uri="https://chatgpt.com/cb",
            redirect_uri_provided_explicitly=True,
            resource=None,
        )
        store.put_pending("TXN", "c1", params.model_dump_json(), ttl=60)
        return handler

    def test_get_renders_form(self):
        handler = self._setup()
        resp = _run(handler(_get_request("/oauth/consent", b"txn=TXN")))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"txn", resp.body)
        self.assertIn(b"password", resp.body)

    def test_post_wrong_passphrase_rejected(self):
        handler = self._setup()
        resp = _run(handler(_post_request({"txn": "TXN", "passphrase": "wrong"})))
        self.assertEqual(resp.status_code, 403)

    def test_post_correct_passphrase_redirects_with_code(self):
        handler = self._setup()
        resp = _run(handler(_post_request({"txn": "TXN", "passphrase": "hunter2"})))
        self.assertIn(resp.status_code, (302, 303, 307))
        self.assertIn("code=", resp.headers["location"])
        self.assertIn("state=st", resp.headers["location"])


if __name__ == "__main__":
    unittest.main()
