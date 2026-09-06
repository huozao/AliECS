from __future__ import annotations

import json
import os
import sys
import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "backend-api"))


class MarketSnapshotContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from app.routers import market_snapshot
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"backend dependencies unavailable: {exc}") from exc
        cls.module = market_snapshot

    def test_missing_file_returns_safe_empty_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"MARKET_SNAPSHOT_FILE": str(Path(temp) / "missing.json")}):
                result = self.module.market_snapshot(limit=200, _={})
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["rows"], [])
        self.assertNotIn("path", result)

    def test_reader_whitelists_private_row_fields_and_honors_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "latest.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "status": "ok",
                "rows": [
                    {"au_symbol": "SHFE.au2612", "source_status": "ok", "secret": "must-not-leak"},
                    {"au_symbol": "SHFE.au2610", "source_status": "stale"},
                ],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"MARKET_SNAPSHOT_FILE": str(path)}):
                result = self.module.market_snapshot(limit=1, _={})
        self.assertEqual(result["contract_count"], 1)
        self.assertEqual(result["rows"][0]["au_symbol"], "SHFE.au2612")
        self.assertNotIn("secret", result["rows"][0])

    def test_ingest_requires_separate_token_and_writes_atomic_public_shape(self) -> None:
        from starlette.requests import Request

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "latest.json"
            scope = {"type": "http", "method": "POST", "path": "/v1/internal/market/snapshot", "headers": [(b"content-length", b"180")]}
            request = Request(scope)
            body = {"schema_version": 1, "rows": [{"au_symbol": "SHFE.au2612", "secret": "no"}]}
            with patch.dict(os.environ, {"MARKET_SNAPSHOT_INGEST_TOKEN": "test-token", "MARKET_SNAPSHOT_FILE": str(path)}):
                response = asyncio.run(self.module.ingest_market_snapshot(request, body, "test-token"))
            self.assertTrue(response["ok"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("secret", saved["rows"][0])

    def test_ingest_is_disabled_without_server_token(self) -> None:
        from fastapi import HTTPException
        from starlette.requests import Request

        request = Request({"type": "http", "method": "POST", "path": "/v1/internal/market/snapshot", "headers": []})
        with patch.dict(os.environ, {"MARKET_SNAPSHOT_INGEST_TOKEN": ""}):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(self.module.ingest_market_snapshot(request, {"schema_version": 1, "rows": []}, ""))
        self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
