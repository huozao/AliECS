"""Backend smoke test: login -> features -> couple access -> healthz.

Skips itself (instead of failing) when DATABASE_URL is not configured, so it
only runs meaningfully in the CI migration-dry-run job which provisions a
real Postgres service container and applies all migrations first.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_main():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import main

    return main


class BackendSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        if not os.environ.get("DATABASE_URL"):
            self.skipTest("smoke test requires a real DATABASE_URL")

    def test_healthz_is_ok_against_real_db(self) -> None:
        main = load_main()
        client = TestClient(main.app)

        resp = client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["database"]["ok"])

    def test_login_with_bootstrap_admin_then_features(self) -> None:
        main = load_main()
        client = TestClient(main.app)

        username = os.environ["ADMIN_BOOTSTRAP_USERNAME"]
        password = os.environ["ADMIN_BOOTSTRAP_PASSWORD"]

        resp = client.post("/v1/auth/login", json={"username": username, "password": password})
        self.assertEqual(resp.status_code, 200, resp.text)
        token = resp.json()["token"]

        headers = {"Authorization": f"Bearer {token}"}
        resp = client.get("/v1/features", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = client.get("/couple/access", headers=headers)
        self.assertIn(resp.status_code, (200, 403))


if __name__ == "__main__":
    unittest.main()
