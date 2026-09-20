"""Measure the realtime chain against the 500ms polling budget it runs on.

The page polls every 500ms.  If one poll costs more than that the requests
queue behind each other and the screen falls further behind wall clock the
longer it runs, so "it renders" is not enough — the cost has to be measured on
the same fixture, machine and browser, with raw samples kept rather than a
single timing.

Everything is local: a temporary Gold archive, Gold's read API on loopback and
the AliECS gateway in-process.  Nothing here touches production or the network.
"""
from __future__ import annotations

from datetime import datetime, timezone
import http.server
import importlib
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time
import unittest

from market_realtime_fixture import CONTRACTS, WINDOW_MINUTES

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_TOKEN = "synthetic-performance-token"
#: The page's steady cadence. One poll has to fit inside it.
POLL_BUDGET_MS = 500
SAMPLES = 30


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


class RealtimePerformanceTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        source = os.getenv("V6_GOLD_SOURCE")
        if not source:
            raise unittest.SkipTest("V6_GOLD_SOURCE not configured for cross-repository integration")
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory(prefix="realtime-performance-")
        sys.path.insert(0, source)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from gold_spread_monitor.review_archive import ReviewArchive
        from gold_spread_monitor.review_archive_api import make_handler
        import market_realtime_archive_fixture as fixture

        cls.window_end = datetime.now(timezone.utc).replace(microsecond=0)
        cls.archive_root = Path(cls.tmp.name) / "archive"
        archive = ReviewArchive(cls.archive_root)
        cls.fixture_info = fixture.populate(archive, cls.window_end)

        cls.gold = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cls.archive_root, ARCHIVE_TOKEN))
        threading.Thread(target=cls.gold.serve_forever, daemon=True).start()
        cls.gold_url = f"http://127.0.0.1:{cls.gold.server_port}"

        os.environ["MARKET_REVIEW_ARCHIVE_URL"] = cls.gold_url
        os.environ["MARKET_REVIEW_ARCHIVE_TOKEN"] = ARCHIVE_TOKEN
        os.environ["MARKET_SNAPSHOT_FILE"] = str(Path(cls.tmp.name) / "latest.json")
        os.environ["AUTH_TOKEN_SECRET"] = "synthetic-performance-secret"

        cls.saved_modules = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
        for key in cls.saved_modules:
            del sys.modules[key]
        cls.backend_root = str(ROOT / "services/backend-api")
        sys.path.insert(0, cls.backend_root)
        core = importlib.import_module("app.core")
        market = importlib.import_module("app.routers.market_snapshot")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(market.router, prefix="/api")
        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.headers = {"Authorization": "Bearer " + core._encode_token(dict(
            uid=7, sub="synthetic-reviewer", tv=1,
            permissions=["market.read"], exp=int(time.time()) + 3600))}
        cls.report: dict[str, dict] = {}

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.gold.shutdown(); cls.gold.server_close()
        for key in list(sys.modules):
            if key == "app" or key.startswith("app."):
                del sys.modules[key]
        sys.modules.update(cls.saved_modules)
        if cls.backend_root in sys.path:
            sys.path.remove(cls.backend_root)
        for variable in ("MARKET_REVIEW_ARCHIVE_URL", "MARKET_REVIEW_ARCHIVE_TOKEN",
                         "MARKET_SNAPSHOT_FILE", "AUTH_TOKEN_SECRET"):
            os.environ.pop(variable, None)
        destination = os.getenv("MARKET_TEST_PERFORMANCE_REPORT")
        if destination and cls.report:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_text(json.dumps(cls.report, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
        cls.tmp.cleanup()

    def _measure(self, label: str, params: dict) -> dict:
        samples, sizes = [], []
        for _ in range(SAMPLES):
            started = time.perf_counter()
            response = self.client.get("/api/v1/market/realtime", params=params,
                                       headers=self.headers)
            samples.append((time.perf_counter() - started) * 1000)
            self.assertEqual(response.status_code, 200, response.text)
            sizes.append(len(response.content))
        measurement = {
            "samples": SAMPLES,
            "query_ms": [round(value, 2) for value in samples],
            "query_ms_p50": round(percentile(samples, 0.50), 2),
            "query_ms_p95": round(percentile(samples, 0.95), 2),
            "bytes": sizes,
            "bytes_p50": percentile([float(size) for size in sizes], 0.50),
            "bytes_p95": percentile([float(size) for size in sizes], 0.95),
            "contracts": len(CONTRACTS),
            "window_minutes": params.get("window_minutes"),
            "budget_ms": POLL_BUDGET_MS,
        }
        type(self).report[label] = measurement
        return measurement

    def test_first_screen_cost_is_measured_and_reported(self):
        """The full-window request is the first screen and after every reset."""
        measurement = self._measure("first_screen", {"window_minutes": WINDOW_MINUTES})
        self.assertEqual(len(measurement["query_ms"]), SAMPLES)
        self.assertGreater(measurement["bytes_p50"], 0)
        # Recorded, not asserted against the budget: a full window is fetched
        # once per reset, not every 500ms.
        print(f"\nfirst screen: p50={measurement['query_ms_p50']}ms "
              f"p95={measurement['query_ms_p95']}ms "
              f"bytes_p50={int(measurement['bytes_p50'])}")

    def test_steady_increment_fits_the_polling_budget(self):
        """The 500ms poll is the increment, and it must not queue up."""
        full = self.client.get("/api/v1/market/realtime",
                               params={"window_minutes": WINDOW_MINUTES},
                               headers=self.headers).json()
        watermark = full["next_since"]
        self.assertIsNotNone(watermark, "a non-truncated window must state its watermark")
        measurement = self._measure("steady_increment",
                                    {"window_minutes": WINDOW_MINUTES, "since": watermark})
        print(f"\nsteady increment: p50={measurement['query_ms_p50']}ms "
              f"p95={measurement['query_ms_p95']}ms "
              f"bytes_p50={int(measurement['bytes_p50'])}")
        self.assertLess(measurement["query_ms_p95"], POLL_BUDGET_MS,
                        f"a steady poll must fit in {POLL_BUDGET_MS}ms or requests queue: "
                        f"{measurement['query_ms']}")

    def test_increment_is_far_cheaper_than_the_whole_window(self):
        """Otherwise the increment is not paying for itself."""
        full = self.report.get("first_screen") or self._measure(
            "first_screen", {"window_minutes": WINDOW_MINUTES})
        delta = self.report.get("steady_increment")
        if delta is None:
            body = self.client.get("/api/v1/market/realtime",
                                   params={"window_minutes": WINDOW_MINUTES},
                                   headers=self.headers).json()
            delta = self._measure("steady_increment",
                                  {"window_minutes": WINDOW_MINUTES, "since": body["next_since"]})
        self.assertLess(delta["bytes_p50"], full["bytes_p50"] / 2,
                        "an increment must be substantially smaller than the window")

    def test_order_transitions_survive_the_measured_path(self):
        """Load must never be reduced by dropping order changes."""
        body = self.client.get("/api/v1/market/realtime",
                               params={"window_minutes": WINDOW_MINUTES},
                               headers=self.headers).json()
        for contract in CONTRACTS:
            statuses = [row["orders"][0]["status"] for row in body["order_series"][contract]]
            self.assertIn("CANCELLED｜已撤单", statuses, contract)
            self.assertIn("ACTIVE｜挂单有效", statuses, contract)
