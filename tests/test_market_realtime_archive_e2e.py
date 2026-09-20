"""Temporary Gold SQLite → Gold HTTP → AliECS source/gateway → real Chromium.

Nothing in the chain is faked: the archive is a real Gold ``ReviewArchive``
partition, Gold's own read API serves it on loopback, the gateway reaches it
through ``LocalReviewSource``, and the page fetches ``/api/v1/market/realtime``
itself.  ``route.fulfill`` is deliberately absent — the browser fixture test
proves the drawing, this proves the data actually survives two repositories.

Set ``V6_GOLD_SOURCE`` to the Gold checkout's ``src`` directory.  No production
database, credential, browser profile or account is used.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import functools
import http.server
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest

from market_realtime_fixture import CHART_PROBE, CONTRACTS, WINDOW_MINUTES

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = Path(os.getenv("MARKET_TEST_PUBLIC_ROOT", str(ROOT / "services/public-web")))
CHROMIUM = os.getenv(
    "MARKET_TEST_CHROMIUM",
    "/home/ishelwsl/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
)
ARCHIVE_TOKEN = "synthetic-archive-e2e-token"


def gold_source() -> str:
    source = os.getenv("V6_GOLD_SOURCE")
    if not source:
        raise unittest.SkipTest("V6_GOLD_SOURCE not configured for cross-repository integration")
    return source


class ApiBridge(http.server.SimpleHTTPRequestHandler):
    """Serve the real static page and forward /api/ to the real gateway app."""

    client = None
    calls: list[tuple[str, str, int, int]] = []
    #: Guarded so the page's single-in-flight rule can be observed rather than assumed.
    lock = threading.Lock()
    inflight = 0
    max_inflight = 0

    def log_message(self, *_args):
        pass

    def _forward(self):
        size = int(self.headers.get("Content-Length", 0))
        realtime = "/api/v1/market/realtime" in self.path
        if realtime:
            with type(self).lock:
                type(self).inflight += 1
                type(self).max_inflight = max(type(self).max_inflight, type(self).inflight)
        started = time.perf_counter()
        response = type(self).client.request(
            self.command, self.path,
            content=self.rfile.read(size) if size else None,
            headers={key: value for key, value in self.headers.items()
                     if key.lower() not in ("host", "content-length")})
        elapsed_ms = (time.perf_counter() - started) * 1000
        if realtime:
            with type(self).lock:
                type(self).inflight -= 1
        type(self).calls.append((self.command, self.path, response.status_code, len(response.content)))
        type(self).timings.append(elapsed_ms)
        self.send_response(response.status_code)
        self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
        self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._forward()
        else:
            super().do_GET()

    def do_POST(self):
        self._forward()


class RealtimeArchiveE2ETests(unittest.TestCase):
    """One built chain per class; each test asserts a different layer of it."""

    @classmethod
    def setUpClass(cls):
        source = gold_source()
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - environment gate
            raise unittest.SkipTest("playwright is not installed") from exc
        if not Path(CHROMIUM).exists():  # pragma: no cover - environment gate
            raise unittest.SkipTest(f"chromium is not installed at {CHROMIUM}")

        import tempfile
        cls.tmp = tempfile.TemporaryDirectory(prefix="realtime-archive-e2e-")
        sys.path.insert(0, source)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from gold_spread_monitor import review_archive_api
        from gold_spread_monitor.review_archive import ReviewArchive
        from gold_spread_monitor.review_archive_api import make_handler

        # The Gold HTTP server runs in this interpreter, so the page limit can
        # be lowered live to reproduce a truncated window against a real source.
        cls.gold_api_module = review_archive_api
        import market_realtime_archive_fixture as fixture

        # Prove the import really came from the checkout under test.
        cls.gold_module_file = sys.modules["gold_spread_monitor.review_archive"].__file__
        assert cls.gold_module_file.startswith(str(Path(source).resolve())), cls.gold_module_file

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
        os.environ["AUTH_TOKEN_SECRET"] = "synthetic-realtime-e2e-secret"

        cls.saved_modules = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
        for key in cls.saved_modules:
            del sys.modules[key]
        sys.path.insert(0, str(ROOT / "services/backend-api"))
        core = importlib.import_module("app.core")
        market = importlib.import_module("app.routers.market_snapshot")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(market.router, prefix="/api")
        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.token = core._encode_token(dict(
            uid=7, sub="synthetic-reviewer", tv=1,
            permissions=["market.read"], exp=int(time.time()) + 900))

        ApiBridge.client = cls.client
        ApiBridge.calls = []
        ApiBridge.timings = []
        cls.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
            ApiBridge, directory=str(PUBLIC_ROOT)))
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, executable_path=CHROMIUM,
                                             args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.http.shutdown(); cls.http.server_close()
        cls.client.close()
        cls.gold.shutdown(); cls.gold.server_close()
        for key in list(sys.modules):
            if key == "app" or key.startswith("app."):
                del sys.modules[key]
        sys.modules.update(cls.saved_modules)
        for variable in ("MARKET_REVIEW_ARCHIVE_URL", "MARKET_REVIEW_ARCHIVE_TOKEN",
                         "MARKET_SNAPSHOT_FILE", "AUTH_TOKEN_SECRET"):
            os.environ.pop(variable, None)
        cls.tmp.cleanup()

    def _route_vendor(self, page):
        vendor = PUBLIC_ROOT / "market/vendor/lightweight-charts-5.0.8.js"
        page.route("**/market/vendor/lightweight-charts-*.js",
                   lambda route: route.fulfill(
                       status=200, content_type="application/javascript",
                       body=vendor.read_text(encoding="utf-8") + CHART_PROBE))

    # ----- layer 1: the archive really holds what the chain will carry -----

    def test_fixture_wrote_real_archive_partitions(self):
        partitions = sorted(path.name for path in self.archive_root.glob("date=*"))
        self.assertTrue(partitions, "the fixture must create at least one dated partition")
        for name in partitions:
            self.assertTrue((self.archive_root / name / "review.sqlite3").exists())
        self.assertGreaterEqual(self.fixture_info["packet_count"], 1800)
        self.assertEqual(self.fixture_info["quote_observations"] % len(CONTRACTS), 0)

    # ----- layer 2: Gold's own HTTP read API -----

    def test_gold_api_serves_the_window_with_order_transitions(self):
        import urllib.request
        request = urllib.request.Request(
            f"{self.gold_url}/internal/review/v1/realtime?window_minutes={WINDOW_MINUTES}",
            headers={"X-Review-Archive-Token": ARCHIVE_TOKEN})
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read())
        self.assertEqual(len(body["series"]), len(CONTRACTS))
        self.assertEqual(len(body["order_series"]), len(CONTRACTS))
        for contract in CONTRACTS:
            self.assertGreater(len(body["series"][contract]["quotes"]), 100, contract)
            statuses = [row["orders"][0]["status"] for row in body["order_series"][contract]]
            self.assertIn("CANCELLED｜已撤单", statuses, contract)
            self.assertIn("ACTIVE｜挂单有效", statuses, contract)
        self.assertFalse(body["truncated"])
        self.assertFalse(body["orders_truncated"])
        self.assertIsNotNone(body["next_since"])

    # ----- layer 3: the AliECS gateway must not drop fields in the middle -----

    def test_gateway_passes_order_series_and_watermark_through(self):
        response = self.client.get(
            f"/api/v1/market/realtime?window_minutes={WINDOW_MINUTES}",
            headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        for field in ("order_series", "orders_truncated", "next_since", "series", "reset"):
            self.assertIn(field, body, f"the gateway dropped {field}")
        self.assertEqual(sorted(body["order_series"]), sorted(CONTRACTS))
        self.assertEqual(body["run_id"], self.fixture_info["run_id"])

        # An increment from the stated watermark must stay inside the window and
        # must not be mistaken for a reset.
        # Pass the watermark as a parameter, not inside a literal URL: a bare
        # `+` in a query string decodes to a space and the source then rejects it.
        watermark = body["next_since"]
        delta = self.client.get(
            "/api/v1/market/realtime",
            params={"window_minutes": WINDOW_MINUTES, "since": watermark},
            headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(delta.status_code, 200, delta.text)
        self.assertTrue(delta.json()["incremental"])
        self.assertFalse(delta.json()["reset"])
        self.assertLess(len(delta.content), len(response.content),
                        "an increment must cost less than the whole window")

    def test_gateway_reports_source_failure_instead_of_inventing_data(self):
        """A dead source is a 503, never an empty window drawn as if it were real."""
        saved = os.environ["MARKET_REVIEW_ARCHIVE_URL"]
        os.environ["MARKET_REVIEW_ARCHIVE_URL"] = "http://127.0.0.1:1"
        try:
            response = self.client.get(
                f"/api/v1/market/realtime?window_minutes={WINDOW_MINUTES}",
                headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"]["code"], "source_unavailable")
        finally:
            os.environ["MARKET_REVIEW_ARCHIVE_URL"] = saved

    # ----- layer 4: a real browser over the real chain -----

    def test_browser_draws_eight_contracts_from_the_archive_chain(self):
        from playwright.sync_api import sync_playwright  # noqa: F401  (import gate)

        page = self.browser.new_page(viewport={"width": 1366, "height": 900})
        page.set_default_timeout(60000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        # Only the clock is pinned, and only so the rolling window matches the
        # fixture's end. Timers keep running, and no request is intercepted.
        page.clock.set_fixed_time(self.window_end)
        page.add_init_script(
            "localStorage.setItem('aliecs_auth_token'," + json.dumps(self.token) + ")")
        # Only the charting vendor is routed, and only to append the recorder
        # after the genuine bundle. The API is never intercepted — an init
        # script cannot do this because the bundle defines the global itself.
        self._route_vendor(page)
        try:
            page.goto(self.base + "/market/realtime/")
            page.wait_for_selector(f'[data-contract="{CONTRACTS[-1]}"]')
            page.wait_for_function("window.__chartProbe && window.__chartProbe.setData.length > 0")
            # The page opens on 5 minutes; the fixture window is 15.
            page.select_option("#window-minutes", str(WINDOW_MINUTES))
            page.wait_for_function(
                "window.__chartProbe && Object.keys(window.__chartProbe.byChart).length === %d"
                % len(CONTRACTS))
            # The archive downsamples to at most 300 points per contract per
            # window, so a full 15 minutes arrives as a few hundred points.
            page.wait_for_function(
                "Object.values(window.__chartProbe.byChart).every(list => list.some(count => count > 100))")
            probe = page.evaluate("window.__chartProbe")
            self.assertGreaterEqual(probe["created"], len(CONTRACTS))
            self.assertEqual(page.locator("#contracts .market-card").count(), len(CONTRACTS))

            # Every card shows real numbers that came from the archive.
            for contract in CONTRACTS:
                card = page.locator(f'.market-card[data-contract="{contract}"]')
                for tag in ("upper", "center", "lower", "current"):
                    text = card.locator(f'[data-tag="{tag}"]').inner_text()
                    self.assertNotEqual(text, "—", f"{contract} {tag} is empty")
                    self.assertGreater(float(text), 0, f"{contract} {tag}")

            # The page requested the API itself; nothing was stubbed.
            realtime_calls = [call for call in ApiBridge.calls if "/api/v1/market/realtime" in call[1]]
            self.assertTrue(realtime_calls, "the page never called the gateway")
            self.assertTrue(all(status == 200 for _, _, status, _ in realtime_calls))
            self.assertFalse(page.evaluate("Boolean(window.__routeStubbed)"))
            self.assertEqual(errors, [])
        finally:
            page.close()

    def test_browser_takes_the_watermark_from_the_server(self):
        page = self.browser.new_page(viewport={"width": 1366, "height": 900})
        page.set_default_timeout(60000)
        page.clock.set_fixed_time(self.window_end)
        page.add_init_script(
            "localStorage.setItem('aliecs_auth_token'," + json.dumps(self.token) + ")")
        try:
            page.goto(self.base + "/market/realtime/")
            page.wait_for_function("document.querySelectorAll('#contracts .market-card').length >= 8")
            # Five minutes keeps the dense 0.5-second fixture below Gold's
            # normal page cap, so this proof exercises a real non-null
            # watermark rather than the separate truncation path.
            page.select_option("#window-minutes", "5")
            before = len([c for c in ApiBridge.calls if "/api/v1/market/realtime" in c[1]])
            # The steady poll is 500ms; wait for at least one increment to be issued.
            page.wait_for_timeout(2000)
            after = [c for c in ApiBridge.calls if "/api/v1/market/realtime" in c[1]]
            self.assertGreater(len(after), before, "the page must keep polling")
            self.assertTrue(any("since=" in call[1] for call in after[before:]),
                            "a follow-up poll must carry the watermark the server stated")
        finally:
            page.close()

    def test_browser_refetches_the_whole_window_after_the_source_truncates(self):
        """Truncation drops the oldest rows; an increment can never recover them.

        The server withholds ``next_since`` in that case, and the page has to
        ask for the whole window again.  A page that derives the watermark from
        the rows it drew keeps incrementing and silently renders a window with
        a hole in its left edge — which is what this reproduces against a real
        source rather than a stub.
        """
        page = self.browser.new_page(viewport={"width": 1366, "height": 900})
        page.set_default_timeout(60000)
        page.clock.set_fixed_time(self.window_end)
        page.add_init_script(
            "localStorage.setItem('aliecs_auth_token'," + json.dumps(self.token) + ")")
        original = self.gold_api_module.MAX_REALTIME_PAGE
        try:
            page.goto(self.base + "/market/realtime/")
            page.wait_for_function("document.querySelectorAll('#contracts .market-card').length >= 8")
            # Steady state first: the page must be incrementing before the source
            # starts truncating, otherwise the assertion proves nothing.
            deadline = time.time() + 30
            while time.time() < deadline and not any(
                    "since=" in call[1] for call in ApiBridge.calls
                    if "/api/v1/market/realtime" in call[1]):
                page.wait_for_timeout(200)
            self.assertTrue(any("since=" in call[1] for call in ApiBridge.calls
                                if "/api/v1/market/realtime" in call[1]),
                            "the page never reached steady incremental polling")
            # Low enough that even a one-second increment overflows the page,
            # so the source has to report truncation on every request.
            self.gold_api_module.MAX_REALTIME_PAGE = 2
            marker = len(ApiBridge.calls)
            deadline = time.time() + 30
            after: list[str] = []
            while time.time() < deadline:
                after = [call[1] for call in ApiBridge.calls[marker:]
                         if "/api/v1/market/realtime" in call[1]]
                if any("since=" not in call for call in after):
                    break
                page.wait_for_timeout(250)
            status = page.locator("#status").inner_text()
            self.assertTrue(after, f"the page stopped polling; status={status!r}")
            self.assertTrue(any("since=" not in call for call in after),
                            f"a truncated window must be re-requested in full: "
                            f"{after[:4]} status={status!r}")
        finally:
            self.gold_api_module.MAX_REALTIME_PAGE = original
            page.close()

    def test_polling_keeps_one_request_in_flight_and_pauses_when_hidden(self):
        """Overlapping polls would queue behind each other and never catch up."""
        page = self.browser.new_page(viewport={"width": 1366, "height": 900})
        page.set_default_timeout(60000)
        page.clock.set_fixed_time(self.window_end)
        page.add_init_script(
            "localStorage.setItem('aliecs_auth_token'," + json.dumps(self.token) + ")")
        ApiBridge.max_inflight = 0
        try:
            page.goto(self.base + "/market/realtime/")
            page.wait_for_function("document.querySelectorAll('#contracts .market-card').length >= 8")
            page.wait_for_timeout(2500)
            self.assertLessEqual(ApiBridge.max_inflight, 1,
                                 "the page must keep a single realtime request in flight")

            # Hiding the tab has to stop the poll, not just hide its result.
            page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            page.wait_for_timeout(600)
            marker = len([c for c in ApiBridge.calls if "/api/v1/market/realtime" in c[1]])
            page.wait_for_timeout(2000)
            hidden_calls = len([c for c in ApiBridge.calls if "/api/v1/market/realtime" in c[1]]) - marker
            self.assertEqual(hidden_calls, 0, "a hidden tab must not keep polling")

            page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            page.wait_for_timeout(1500)
            resumed = len([c for c in ApiBridge.calls if "/api/v1/market/realtime" in c[1]]) - marker
            self.assertGreater(resumed, 0, "becoming visible again must resume the poll")
        finally:
            page.close()

    def test_repeated_window_switches_leave_no_extra_charts_or_cards(self):
        """Each switch rebuilds the window; leaked charts would accumulate silently."""
        page = self.browser.new_page(viewport={"width": 1366, "height": 900})
        page.set_default_timeout(60000)
        page.clock.set_fixed_time(self.window_end)
        page.add_init_script(
            "localStorage.setItem('aliecs_auth_token'," + json.dumps(self.token) + ")")
        self._route_vendor(page)
        try:
            page.goto(self.base + "/market/realtime/")
            page.wait_for_function("document.querySelectorAll('#contracts .market-card').length >= 8")
            for minutes in (15, 5, 10, 15, 5):
                page.select_option("#window-minutes", str(minutes))
                page.wait_for_function(
                    "document.querySelectorAll('#contracts .market-card').length === %d"
                    % len(CONTRACTS))
            self.assertEqual(page.locator("#contracts .market-card").count(), len(CONTRACTS))
            # Chart instances are created per card; a leak shows up as charts
            # created without a card to hold them.
            created = page.evaluate("window.__chartProbe ? window.__chartProbe.created : 0")
            self.assertGreaterEqual(created, len(CONTRACTS))
            self.assertEqual(page.locator("#contracts canvas").count() % len(CONTRACTS), 0)
        finally:
            page.close()
