"""Split-page browser behaviour with a local static server and synthetic API."""
from __future__ import annotations

import functools
import http.server
import json
import os
from pathlib import Path
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MarketSplitBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest('playwright is not installed') from exc
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / 'services/public-web'))
        cls.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, executable_path=os.getenv('MARKET_TEST_CHROMIUM', '/home/ishelwsl/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome'), args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.http.shutdown(); cls.http.server_close()

    def page(self, status=200):
        page = self.browser.new_page(viewport={'width': 1366, 'height': 900})
        page.set_default_timeout(3000)
        calls = []
        event = {'event_id': 'event-1', 'run_id': 'run-a', 'sequence': 1, 'position_id': 'p-1',
                 'trading_day': '2026-09-10', 'occurred_at': '2026-09-10T01:00:00Z',
                 'event_type': 'TARGET_FILL_CONFIRMED', 'symbol': 'SHFE.au2612'}
        def route(route):
            url = route.request.url; calls.append(url)
            if status != 200:
                route.fulfill(status=status, content_type='application/json', body=json.dumps({'detail': 'synthetic'})); return
            if url.endswith('/realtime') or '/realtime?' in url:
                body = {'window_minutes': 15, 'window_start': '2026-09-10T00:45:00Z', 'window_end': '2026-09-10T01:00:00Z',
                        'run_id': 'run-a', 'quotes': [{'contract':'SHFE.au2612','last_price':960,'source_time':'2026-09-10T01:00:00Z'}],
                        'bands': [{'contract':'SHFE.au2612','center':960,'lower':959,'upper':961,'source_time':'2026-09-10T01:00:00Z'}],
                        'series': {'SHFE.au2612': {'quotes':[{'last_price':960,'observed_at':'2026-09-10T01:00:00Z'}], 'bands':[]}},
                        'orders': [], 'hedge_ranking': [], 'freshness': {}, 'next_cursor': None, 'truncated': False}
            elif '/events/index?' in url: body = {'items':[event], 'has_more':False, 'next_cursor':None, 'effective_trading_day':'2026-09-10'}
            elif '/detail' in url: body = {'event':event, 'position':{'unresolved':True,'model':{},'account':{}}, 'run_id':'run-a', 'position_id':'p-1', 'lifecycle':[event], 'series':{'target':{'quotes':[{'last_price':960,'observed_at':'2026-09-10T01:00:00Z'}]},'hedge':{'quotes':[]}}}
            else: body = {}
            route.fulfill(status=200, content_type='application/json', body=json.dumps(body))
        page.route('**/api/**', route)
        page.add_init_script("localStorage.setItem('aliecs_auth_token','synthetic-token')")
        return page, calls

    def test_realtime_uses_one_bounded_endpoint_and_index_defers_detail(self):
        base = f'http://127.0.0.1:{self.http.server_port}'
        page, calls = self.page()
        page.goto(base + '/market/realtime/')
        page.wait_for_selector('[data-contract="SHFE.au2612"]')
        self.assertTrue(any('/market/realtime' in call for call in calls))
        self.assertFalse(any('/events' in call or '/latest' in call for call in calls))
        page.close()
        page, calls = self.page()
        page.goto(base + '/market/today/')
        page.wait_for_selector('[data-event="event-1"]')
        self.assertFalse(any('/detail' in call for call in calls))
        page.locator('[data-event="event-1"]').click()
        page.wait_for_function("document.querySelector('#detail').textContent.includes('p-1')")
        self.assertEqual(sum('/detail' in call for call in calls), 1)
        page.close()

    def test_direct_unauthorized_page_offers_login(self):
        base = f'http://127.0.0.1:{self.http.server_port}'
        for status, message in ((401, '登录已过期'), (403, '没有市场查看权限')):
            page, _ = self.page(status=status)
            page.goto(base + '/market/history/')
            page.wait_for_function("!document.querySelector('#login').hidden")
            self.assertIn(message, page.locator('#events').inner_text())
            page.close()
