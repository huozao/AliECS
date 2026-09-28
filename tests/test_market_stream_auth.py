from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1] / "services/backend-api"


class MarketStreamAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND))
        cls.module = importlib.import_module("app.routers.market_stream")
        cls.snapshot_module = importlib.import_module("app.routers.market_snapshot")

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.remove(str(BACKEND))

    def test_requires_login_and_current_market_permission(self):
        app = FastAPI()
        app.include_router(self.module.router)
        client = TestClient(app)
        app.dependency_overrides[self.module.require_login] = lambda: (_ for _ in ()).throw(HTTPException(401))
        self.assertEqual(client.get("/v1/market/stream-token").status_code, 401)
        app.dependency_overrides[self.module.require_login] = lambda: {"uid": 7, "permissions": ["market.read"]}
        with patch.object(self.module, "_user_roles_permissions", return_value=([], [])):
            self.assertEqual(client.get("/v1/market/stream-token").status_code, 403)

    def test_token_is_short_lived_and_limited_to_market_channel(self):
        app = FastAPI()
        app.include_router(self.module.router)
        app.dependency_overrides[self.module.require_login] = lambda: {"uid": 7, "permissions": ["market.read"]}
        client = TestClient(app)
        secret = "s" * 64
        with patch.object(self.module, "_user_roles_permissions", return_value=([], ["market.read"])):
            with patch.dict("os.environ", {"GSM_CENTRIFUGO_TOKEN_SECRET": secret}):
                response = client.get("/v1/market/stream-token")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        header, payload, signature = body["token"].split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expect = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        self.assertEqual(signature, expect)
        self.assertEqual(claims["sub"], "7")
        self.assertEqual(claims["channels"], ["gold:market"])
        self.assertEqual(claims["aud"], "gold-market-stream")
        self.assertEqual(claims["iss"], "aliecs-market")
        self.assertLessEqual(claims["exp"] - claims["iat"], 60)

    def test_bootstrap_requires_market_permission_and_private_source(self):
        app = FastAPI()
        app.include_router(self.snapshot_module.router)
        client = TestClient(app)
        app.dependency_overrides[self.snapshot_module.require_login] = lambda: {"uid": 7, "permissions": []}
        with patch.object(self.module, "_user_roles_permissions", return_value=([], [])):
            self.assertEqual(client.get("/v1/market/bootstrap?window_minutes=5").status_code, 403)
        app.dependency_overrides[self.snapshot_module.require_login] = lambda: {"uid": 7, "permissions": ["market.read"]}
        with patch.object(self.module, "_user_roles_permissions", return_value=([], ["market.read"])):
            with patch.object(self.snapshot_module, "_source_get", return_value={
                "schema_version": "gold-display-bootstrap/v1", "run_id": "r",
                "stream_epoch": "e", "source_sequence": 0, "continuous": True,
                "window_minutes": 5, "events": []}):
                response = client.get("/v1/market/bootstrap?window_minutes=5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run_id"], "r")
        with patch.object(self.module, "_user_roles_permissions", return_value=([], ["market.read"])):
            self.assertEqual(client.get("/v1/market/bootstrap?window_minutes=7").status_code, 422)

    def test_bootstrap_is_gzipped_only_when_client_accepts_it(self):
        app = FastAPI()
        app.include_router(self.snapshot_module.router)
        client = TestClient(app)
        app.dependency_overrides[self.snapshot_module.require_login] = lambda: {"uid": 7, "permissions": ["market.read"]}
        sourced = {"schema_version": "gold-display-bootstrap/v1", "run_id": "r",
                   "stream_epoch": "e", "source_sequence": 2, "continuous": True,
                   "window_minutes": 5, "events": [{"contract": "SHFE.au2612", "price": 1.5}] * 200}
        with patch.object(self.module, "_user_roles_permissions", return_value=([], ["market.read"])), \
                patch.object(self.snapshot_module, "_source_get", return_value=sourced):
            zipped = client.get("/v1/market/bootstrap?window_minutes=5",
                                headers={"Accept-Encoding": "br, gzip;q=0.8"})
            plain = client.get("/v1/market/bootstrap?window_minutes=5",
                               headers={"Accept-Encoding": "identity"})
            refused = client.get("/v1/market/bootstrap?window_minutes=5",
                                 headers={"Accept-Encoding": "gzip;q=0"})
        self.assertEqual(zipped.headers["content-encoding"], "gzip")
        self.assertLess(int(zipped.headers["content-length"]), len(plain.content))
        self.assertEqual(zipped.json(), sourced)
        for response in (zipped, plain, refused):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["vary"], "Accept-Encoding")
            self.assertTrue(response.headers["content-type"].startswith("application/json"))
        self.assertNotIn("content-encoding", plain.headers)
        self.assertNotIn("content-encoding", refused.headers)
        self.assertEqual(plain.json(), sourced)
