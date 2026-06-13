from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

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
    for name in ("config", "store"):
        s = importlib.util.spec_from_file_location(
            f"{_PKG}.oauth.{name}", SVC / "oauth" / f"{name}.py"
        )
        m = importlib.util.module_from_spec(s)
        sys.modules[f"{_PKG}.oauth.{name}"] = m
        s.loader.exec_module(m)
        mods[name] = m
    return mods


class StoreTests(unittest.TestCase):
    def _store(self):
        return load_oauth()["store"].OAuthStore(":memory:", "pepper-secret")

    def test_client_roundtrip(self):
        s = self._store()
        s.put_client("c1", '{"client_id":"c1"}')
        self.assertEqual(s.get_client("c1"), '{"client_id":"c1"}')
        self.assertIsNone(s.get_client("missing"))

    def test_pending_take_is_one_shot(self):
        s = self._store()
        s.put_pending("t1", "c1", '{"p":1}', ttl=60)
        self.assertEqual(s.take_pending("t1"), ("c1", '{"p":1}'))
        self.assertIsNone(s.take_pending("t1"))

    def test_pending_expired_returns_none(self):
        s = self._store()
        s.put_pending("t2", "c1", "{}", ttl=-1)
        self.assertIsNone(s.take_pending("t2"))

    def test_hashed_token_roundtrip_and_expiry(self):
        s = self._store()
        s.put_hashed("access_tokens", "rawtok", '{"client_id":"c1"}', ttl=60)
        self.assertEqual(s.get_hashed("access_tokens", "rawtok"), '{"client_id":"c1"}')
        self.assertIsNone(s.get_hashed("access_tokens", "wrong"))
        s.delete_hashed("access_tokens", "rawtok")
        self.assertIsNone(s.get_hashed("access_tokens", "rawtok"))

    def test_raw_value_not_stored_in_clear(self):
        s = self._store()
        s.put_hashed("access_tokens", "supersecret", "{}", ttl=60)
        rows = s._conn.execute("SELECT k FROM access_tokens").fetchall()
        self.assertNotIn("supersecret", [r[0] for r in rows])


if __name__ == "__main__":
    unittest.main()
