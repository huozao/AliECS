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
