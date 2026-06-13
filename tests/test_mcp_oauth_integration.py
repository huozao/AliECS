from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

SVC = Path(__file__).resolve().parents[1] / "services" / "mcp-coding-server" / "app"


def _load_main(env):
    for name in list(sys.modules):
        if name.split(".")[0] == "app" or name.startswith("mcp_oauth_pkg"):
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "app", SVC / "__init__.py", submodule_search_locations=[str(SVC)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["app"] = pkg
    spec.loader.exec_module(pkg)
    main_spec = importlib.util.spec_from_file_location("app.main", SVC / "main.py")
    main = importlib.util.module_from_spec(main_spec)
    sys.modules["app.main"] = main
    with mock.patch.dict(os.environ, env, clear=False):
        main_spec.loader.exec_module(main)
    return main


class IntegrationTests(unittest.TestCase):
    def test_disabled_keeps_healthz_open_no_auth(self):
        main = _load_main({"MCP_OAUTH_ENABLED": "false"})
        app = main.mcp.streamable_http_app()
        client = TestClient(app)
        self.assertEqual(client.get("/healthz").status_code, 200)
        payload = main.server_info_payload()
        self.assertEqual(payload["phase"], "phase-4-oauth")
        self.assertIn("OAuth", payload["note"])

    def test_enabled_serves_metadata_and_consent_and_keeps_healthz_open(self):
        db = os.path.join(tempfile.gettempdir(), "oauth-int.db")
        if os.path.exists(db):
            os.remove(db)
        env = {
            "MCP_OAUTH_ENABLED": "true",
            "MCP_OAUTH_ISSUER": "https://h.xyz/mcp-x",
            "MCP_OAUTH_PASSPHRASE": "pw",
            "MCP_OAUTH_SIGNING_SECRET": "p" * 32,
            "MCP_OAUTH_STORE_PATH": db,
        }
        main = _load_main(env)
        app = main.mcp.streamable_http_app()
        client = TestClient(app)
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.get("/oauth/consent?txn=missing").status_code, 200)
        authz = client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(authz.status_code, 200)
        self.assertIn("authorization_endpoint", authz.json())
        protected = client.get("/.well-known/oauth-protected-resource/mcp-x")
        self.assertEqual(protected.status_code, 200)


if __name__ == "__main__":
    unittest.main()
