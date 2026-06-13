from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

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
    for name in ("config", "store", "provider"):
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


class ProviderTests(unittest.TestCase):
    def _provider(self):
        m = load_oauth()
        cfg = m["config"].OAuthConfig(
            enabled=True,
            issuer_url="https://h.xyz/mcp-x",
            passphrase="pw",
            pepper="p" * 32,
            store_path=":memory:",
            access_ttl=60,
            refresh_ttl=600,
            code_ttl=60,
        )
        store = m["store"].OAuthStore(":memory:", cfg.pepper)
        return m["provider"].AliecsOAuthProvider(cfg, store), cfg

    def _client(self, client_id="c1"):
        return OAuthClientInformationFull(
            client_id=client_id, redirect_uris=["https://chatgpt.com/cb"]
        )

    def _params(self):
        return AuthorizationParams(
            state="st",
            scopes=["coding"],
            code_challenge="chal",
            redirect_uri="https://chatgpt.com/cb",
            redirect_uri_provided_explicitly=True,
            resource=None,
        )

    def test_register_then_get_client(self):
        p, _ = self._provider()
        c = self._client()
        _run(p.register_client(c))
        got = _run(p.get_client("c1"))
        self.assertIsNotNone(got)
        self.assertEqual(got.client_id, "c1")

    def test_authorize_returns_consent_url_and_stores_pending(self):
        p, cfg = self._provider()
        c = self._client()
        _run(p.register_client(c))
        url = _run(p.authorize(c, self._params()))
        self.assertTrue(url.startswith(cfg.issuer_url + "/oauth/consent?txn="))

    def test_complete_then_exchange_code_issues_tokens(self):
        p, _ = self._provider()
        c = self._client()
        _run(p.register_client(c))
        params = self._params()
        redirect = p.complete_authorization("c1", params)
        self.assertIn("code=", redirect)
        code = redirect.split("code=")[1].split("&")[0]
        auth_code = _run(p.load_authorization_code(c, code))
        self.assertIsNotNone(auth_code)
        self.assertEqual(auth_code.code_challenge, "chal")
        tok = _run(p.exchange_authorization_code(c, auth_code))
        self.assertTrue(tok.access_token and tok.refresh_token)
        at = _run(p.load_access_token(tok.access_token))
        self.assertIsNotNone(at)
        self.assertEqual(at.client_id, "c1")
        self.assertIsNone(_run(p.load_authorization_code(c, code)))

    def test_authorization_code_is_bound_to_client(self):
        p, _ = self._provider()
        c = self._client()
        wrong = self._client("c2")
        _run(p.register_client(c))
        redirect = p.complete_authorization("c1", self._params())
        code = redirect.split("code=")[1].split("&")[0]
        self.assertIsNone(_run(p.load_authorization_code(wrong, code)))

    def test_refresh_rotates(self):
        p, _ = self._provider()
        c = self._client()
        _run(p.register_client(c))
        redirect = p.complete_authorization("c1", self._params())
        code = redirect.split("code=")[1].split("&")[0]
        tok = _run(p.exchange_authorization_code(c, _run(p.load_authorization_code(c, code))))
        rt = _run(p.load_refresh_token(c, tok.refresh_token))
        tok2 = _run(p.exchange_refresh_token(c, rt, ["coding"]))
        self.assertTrue(tok2.access_token)
        self.assertIsNone(_run(p.load_refresh_token(c, tok.refresh_token)))

    def test_revoke(self):
        p, _ = self._provider()
        c = self._client()
        _run(p.register_client(c))
        redirect = p.complete_authorization("c1", self._params())
        code = redirect.split("code=")[1].split("&")[0]
        tok = _run(p.exchange_authorization_code(c, _run(p.load_authorization_code(c, code))))
        at = _run(p.load_access_token(tok.access_token))
        _run(p.revoke_token(at))
        self.assertIsNone(_run(p.load_access_token(tok.access_token)))


if __name__ == "__main__":
    unittest.main()
