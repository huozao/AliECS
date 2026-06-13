from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

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
    for name in ("config",):
        s = importlib.util.spec_from_file_location(
            f"{_PKG}.oauth.{name}", SVC / "oauth" / f"{name}.py"
        )
        m = importlib.util.module_from_spec(s)
        sys.modules[f"{_PKG}.oauth.{name}"] = m
        s.loader.exec_module(m)
        mods[name] = m
    return mods


class ConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        cfg = load_oauth()["config"]
        with mock.patch.dict(os.environ, {}, clear=True):
            c = cfg.config_from_env()
        self.assertFalse(c.enabled)

    def test_enabled_and_fields(self):
        cfg = load_oauth()["config"]
        env = {
            "MCP_OAUTH_ENABLED": "true",
            "MCP_OAUTH_ISSUER": "https://h.xyz/mcp-abc/",
            "MCP_OAUTH_PASSPHRASE": "pw",
            "MCP_OAUTH_SIGNING_SECRET": "x" * 32,
            "MCP_OAUTH_STORE_PATH": "/tmp/x.db",
            "MCP_OAUTH_ACCESS_TTL": "120",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            c = cfg.config_from_env()
        self.assertTrue(c.enabled)
        self.assertTrue(c.fully_configured)
        self.assertEqual(c.issuer_url, "https://h.xyz/mcp-abc")
        self.assertEqual(c.access_ttl, 120)


if __name__ == "__main__":
    unittest.main()
