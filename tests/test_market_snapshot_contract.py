from __future__ import annotations

import json
import gzip
import os
import sys
import tempfile
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
        self.assertFalse(result["comparison"]["available"])
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
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "latest.json"
            body = {"schema_version": 1, "rows": [{"au_symbol": "SHFE.au2612", "secret": "no"}]}
            app = FastAPI()
            app.include_router(self.module.router)
            with patch.dict(os.environ, {"MARKET_SNAPSHOT_INGEST_TOKEN": "test-token", "MARKET_SNAPSHOT_FILE": str(path)}):
                with TestClient(app) as client:
                    response = client.post("/v1/internal/market/snapshot", json=body,
                                           headers={"X-Market-Snapshot-Token": "test-token"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["ok"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("secret", saved["rows"][0])
            self.assertFalse(saved["comparison"]["available"])

    def test_ingest_is_disabled_without_server_token(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(self.module.router)
        with patch.dict(os.environ, {"MARKET_SNAPSHOT_INGEST_TOKEN": ""}):
            with TestClient(app) as client:
                response = client.post("/v1/internal/market/snapshot", content=b"not-json")
        self.assertEqual(response.status_code, 503)

    def test_gzip_ingest_authenticates_before_body_parsing_and_preserves_json(self) -> None:
        """Catches framework JSON parsing before token auth and lossy gzip decoding."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(self.module.router)
        body = {"schema_version": "market-review.v1", "model_version": "V6.0",
                "run_id": "gzip-run", "sequence": 7,
                "published_at": "2026-09-10T00:00:00Z", "quotes": [],
                "bands": [], "events": []}
        wire = gzip.compress(json.dumps(body, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8"))
        with patch.dict(os.environ, {"MARKET_SNAPSHOT_INGEST_TOKEN": "test-token"}), \
                patch.object(self.module.market_review, "ingest",
                             side_effect=lambda decoded: {"ok": decoded == body}) as ingest:
            with TestClient(app) as client:
                unauthorized = client.post("/v1/internal/market/snapshot", content=b"bad-gzip",
                    headers={"Content-Encoding": "gzip", "X-Market-Snapshot-Token": "wrong"})
                accepted = client.post("/v1/internal/market/snapshot", content=wire,
                    headers={"Content-Type": "application/json", "Content-Encoding": "gzip",
                             "X-Market-Snapshot-Token": "test-token"})
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertTrue(accepted.json()["ok"])
        ingest.assert_called_once_with(body)

    def test_ingest_rejects_wire_and_decompressed_size_bypass_without_trusting_length(self) -> None:
        """Catches compressed bombs and false small Content-Length bypasses."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(self.module.router)
        limit = 2 * 1024 * 1024
        oversized_json = b'{"value":"' + b"x" * limit + b'"}'
        with patch.dict(os.environ, {"MARKET_SNAPSHOT_INGEST_TOKEN": "test-token"}):
            with TestClient(app) as client:
                wire_too_large = client.post("/v1/internal/market/snapshot",
                    content=oversized_json,
                    headers={"Content-Length": "1", "X-Market-Snapshot-Token": "test-token"})
                inflated_too_large = client.post("/v1/internal/market/snapshot",
                    content=gzip.compress(oversized_json),
                    headers={"Content-Encoding": "gzip",
                             "X-Market-Snapshot-Token": "test-token"})
        self.assertEqual(wire_too_large.status_code, 413)
        self.assertEqual(inflated_too_large.status_code, 413)

    def test_ingest_rejects_malformed_trailing_concatenated_and_unsupported_encodings(self) -> None:
        """Catches accepting ambiguous gzip members or silently ignoring encodings."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(self.module.router)
        member = gzip.compress(b'{"schema_version":1,"rows":[]}')
        cases = [
            (member[:-1], "gzip", 400),
            (member + b"trailing", "gzip", 400),
            (member + member, "gzip", 400),
            (b"{}", "br", 415),
            (b"{", "identity", 400),
            (b"[]", "identity", 422),
        ]
        with patch.dict(os.environ, {"MARKET_SNAPSHOT_INGEST_TOKEN": "test-token"}):
            with TestClient(app) as client:
                statuses = [client.post("/v1/internal/market/snapshot", content=wire,
                    headers={"Content-Encoding": encoding,
                             "X-Market-Snapshot-Token": "test-token"}).status_code
                            for wire, encoding, _ in cases]
        self.assertEqual(statuses, [expected for _, _, expected in cases])


if __name__ == "__main__":
    unittest.main()
