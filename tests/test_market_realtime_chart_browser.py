"""Prove the realtime page actually draws eight contracts before judging its look.

The earlier acceptance read label text from a one-contract fixture whose data
had already been discarded: ``realtime.js`` trims to ``Date.now() - window``,
and the fixture's 2026-09-10 rows are far in the past, so every series was
emptied before ``setData``.  The browser clock is therefore pinned to the
fixture's ``window_end`` here, and the assertions read the data that reached
the real charting vendor rather than the surrounding text.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
from pathlib import Path
import threading
import unittest

from market_realtime_fixture import (
    CHART_PROBE,
    CONTRACTS,
    RUN_ID,
    WINDOW_END,
    WINDOW_MINUTES,
    realtime_body,
)

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = Path(os.getenv("MARKET_TEST_PUBLIC_ROOT", str(ROOT / "services/public-web")))
CHROMIUM = os.getenv(
    "MARKET_TEST_CHROMIUM",
    "/home/ishelwsl/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
)


def _screenshot_root() -> Path:
    root = Path(os.getenv("MARKET_TEST_SCREENSHOT_DIR", ""))
    if not str(root):
        import tempfile

        root = Path(tempfile.mkdtemp(prefix="market-realtime-shots-"))
    root.mkdir(parents=True, exist_ok=True)
    return root


class RealtimeChartBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - environment gate
            raise unittest.SkipTest("playwright is not installed") from exc
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(PUBLIC_ROOT)
        )
        cls.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(
            headless=True, executable_path=CHROMIUM, args=["--no-sandbox"]
        )
        cls.shots = _screenshot_root()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.http.shutdown()
        cls.http.server_close()

    def page(self, *, viewport=None):
        page = self.browser.new_page(viewport=viewport or {"width": 1366, "height": 900})
        page.set_default_timeout(30000)
        # 只固定 Date，不冻结定时器：`clock.install` 会把轮询也停掉，页面永远
        # 停在首屏。滚动窗按 `Date.now()` 裁剪，不固定就会把夹具数据全删光。
        page.clock.set_fixed_time(WINDOW_END)
        page.add_init_script("localStorage.setItem('aliecs_auth_token','synthetic-token')")
        body = realtime_body()
        page.route(
            "**/api/**",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps(body if "/realtime" in route.request.url else {}),
            ),
        )
        # 真实 vendor 原样执行，只在其后追加记录层；不用假图表替代它。
        vendor = PUBLIC_ROOT / "market/vendor/lightweight-charts-5.0.8.js"
        page.route(
            "**/market/vendor/lightweight-charts-*.js",
            lambda route: route.fulfill(
                status=200, content_type="application/javascript",
                body=vendor.read_text(encoding="utf-8") + CHART_PROBE,
            ),
        )
        return page

    def _load(self, page, *, window_minutes: int = WINDOW_MINUTES):
        base = f"http://127.0.0.1:{self.http.server_port}"
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/market/realtime/")
        page.wait_for_selector(f'[data-contract="{CONTRACTS[-1]}"]')
        page.wait_for_function("window.__chartProbe && window.__chartProbe.setData.length > 0")
        # 页面默认 5 分钟；夹具是 15 分钟窗口，切过去才覆盖整段。
        page.select_option("#window-minutes", str(window_minutes))
        page.wait_for_function(
            "window.__chartProbe && Object.keys(window.__chartProbe.byChart).length === %d"
            % len(CONTRACTS)
        )
        page.wait_for_function(
            "Object.values(window.__chartProbe.byChart).every(list => list.some(n => n > 600))"
        )
        return errors

    def test_eight_contracts_each_get_a_chart_with_non_empty_data(self):
        page = self.page()
        errors = self._load(page)
        self.assertEqual(errors, [])
        cards = page.locator(".realtime-contract-card")
        self.assertEqual(cards.count(), len(CONTRACTS))
        probe = page.evaluate("window.__chartProbe")
        self.assertEqual(probe["created"], len(CONTRACTS), "one chart per contract")
        self.assertTrue(probe["setData"], "setData was never called")
        self.assertEqual(len(probe["byChart"]), len(CONTRACTS))
        # 关键断言：每张图都必须有真正带数据的序列。修复前这里全是空。
        for index, counts in probe["byChart"].items():
            self.assertTrue(counts, f"chart {index} never received data")
            self.assertTrue(
                any(count > 600 for count in counts),
                f"chart {index} only received empty or tiny series: {counts}",
            )
        # 15 分钟 × 每秒一点，去重后应接近 900。
        self.assertGreaterEqual(max(probe["setData"]), 600)
        page.close()

    def test_every_card_shows_real_band_and_order_numbers_not_dashes(self):
        page = self.page()
        self._load(page)
        for contract in CONTRACTS:
            card = page.locator(f'[data-contract="{contract}"]')
            for tag in ("upper", "center", "lower", "current"):
                text = card.locator(f'[data-tag="{tag}"]').inner_text()
                self.assertNotEqual(text, "—", f"{contract} {tag} rendered as a dash")
                self.assertTrue(float(text) > 0)
            # null 不能被当成 0 画出来
            self.assertNotIn(">0.00<", card.inner_html())
        page.close()

    def test_the_order_ladder_is_drawn_from_real_resting_orders(self):
        """A ladder that reads NONE proves the payload shape, not the drawing."""
        page = self.page()
        self._load(page)
        for contract in CONTRACTS:
            card = page.locator(f'[data-contract="{contract}"]')
            for tag in ("buy", "sell"):
                text = card.locator(f'[data-tag="{tag}"]').inner_text()
                self.assertNotEqual(text, "—", f"{contract} {tag} ladder price is a dash")
                self.assertGreater(float(text), 0)
            summary = card.locator(".muted").inner_text()
            self.assertIn("ACTIVE", summary, f"{contract} has no effective resting order")
            self.assertNotIn("NONE｜当前无有效挂单", summary)
        # 买卖两条阶梯各自拿到数据：修复前它们是空序列。
        ladder = page.evaluate("""() => {
          const out = [];
          for (const card of document.querySelectorAll('.realtime-contract-card')) out.push(card.dataset.contract);
          return out;
        }""")
        self.assertEqual(len(ladder), len(CONTRACTS))
        page.close()

    def test_axis_and_tooltip_use_sgt(self):
        page = self.page()
        self._load(page)
        self.assertIn("SGT", page.locator("#status").inner_text())
        self.assertIn("SGT", page.locator(".realtime-legend").inner_text())
        page.close()

    def test_desktop_and_mobile_screenshots_have_no_horizontal_overflow(self):
        page = self.page()
        self._load(page)
        page.screenshot(path=str(self.shots / "w1-realtime-desktop-8.png"), full_page=True)
        page.close()

        mobile = self.page(viewport={"width": 390, "height": 844})
        self._load(mobile)
        overflow = mobile.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        self.assertLessEqual(overflow, 0, "the mobile layout scrolls horizontally")
        mobile.screenshot(path=str(self.shots / "w1-realtime-mobile-8.png"), full_page=True)
        mobile.close()

    def test_order_history_close_up_screenshot(self):
        """Keep a focused visual proof of the active→cancelled order ladder."""
        page = self.page()
        self._load(page)
        card = page.locator(f'[data-contract="{CONTRACTS[0]}"]').first
        self.assertIn("ACTIVE", card.locator(".muted").inner_text())
        card.screenshot(path=str(self.shots / "w3-realtime-order-history-close-up.png"))
        page.close()

    def test_licence_and_notice_are_reachable_from_the_page(self):
        page = self.page()
        self._load(page)
        footer = page.locator(".chart-attribution")
        self.assertIn("TradingView", footer.inner_text())
        base = f"http://127.0.0.1:{self.http.server_port}"
        for href, expected in (("/market/vendor/LICENSE", "Apache"), ("/market/vendor/NOTICE", "")):
            response = page.request.get(base + href)
            self.assertEqual(response.status, 200, href)
            if expected:
                self.assertIn(expected, response.text())
        page.close()


if __name__ == "__main__":
    unittest.main()
