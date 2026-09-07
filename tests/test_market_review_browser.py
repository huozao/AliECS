"""Real Chromium interaction against synthetic transport; never a production session."""
from __future__ import annotations
import functools
import http.server
import json
import os
from pathlib import Path
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]

class MarketReviewBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest("playwright is not installed in this CI job") from exc
        handler=functools.partial(http.server.SimpleHTTPRequestHandler,directory=str(ROOT/'services/public-web'))
        cls.http=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(executable_path=os.environ.get('MARKET_TEST_CHROMIUM','/home/ishelwsl/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome'),headless=True,args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.http.shutdown();cls.http.server_close()

    def test_spike_selection_pause_zero_gap_and_bounded_refresh(self):
        page=self.browser.new_page(viewport={'width':390,'height':844})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        snapshot={'schema_version':'market-review.v1','model_version':'V6.0','run_id':'synthetic', 'sequence':6,'synthetic':True,
            'published_at':'2026-09-07T01:00:07Z','received_at':'2026-09-07T01:00:08Z',
            'quotes':[{'contract':'SHFE.au2612','source_time':'2026-09-07T01:00:00Z','last_price':962,'international_price':962,
                'sources':{'usdcnh':{'source_time':'2026-09-07T00:00:00Z','age_seconds':3600}},
                'ohlc':{'bucket_start':'2026-09-07T01:00:00Z','open':962,'high':963,'low':960,'close':962,'low_time':'2026-09-07T01:00:00.125Z'}}],
            'bands':[{'contract':'SHFE.au2612','source_time':'2026-09-07T01:00:00Z','center':962,'lower':961,'upper':963,'international_price':962,'fit_available':True}],
            'unresolved_positions':[{'position_id':'p2','run_id':'previous-run','reasons':[{'reason':'目标腿剩余 1 手'}]}]}
        event={'event_id':'e1','run_id':'synthetic','sequence':1,'position_id':'p1','event_type':'CANDIDATE_SNAPSHOT','occurred_at':'2026-09-07T01:00:00Z','payload':{'decision_id':'d1','candidates':[{'rank':2,'contract':'B','eligible':False,'rejection_reasons':['拒绝']},{'rank':1,'contract':'A','selected':True}]}}
        requests=[]
        def route(r):
            url=r.request.url;requests.append(url)
            if '/auth/me' in url: data={'username':'synthetic-user','display_name':'合成测试账户'}
            elif '/latest' in url: data=snapshot
            elif '/series' in url:data={'quotes':snapshot['quotes'],'bands':snapshot['bands'],'has_more':False}
            elif '/events' in url:data={'events':[event],'next_sequence':1,'has_more':False,'missing_sequence_ranges':[]}
            elif '/positions/' in url:data={'position_id':'p1','run_id':'synthetic','model':{'net_pnl_cny':950},'account':{'status':'UNRESOLVED','legs':{},'fixed_cost_net_cny':None},'events':[event],'decisions':[event],'reasons':[{'reason':'缺账户平仓回报'}]}
            elif '/annotations' in url:data={'annotations':[]}
            elif '/comparison' in url:data={'available':False,'status':'NOT_RUN'}
            else:data={}
            r.fulfill(status=200,content_type='application/json',body=json.dumps(data))
        page.route('**/api/**',route)
        page.goto(f'http://127.0.0.1:{self.http.server_port}/market/')
        page.locator('#review-status').wait_for()
        page.wait_for_function("window.MarketReview && window.MarketReview.state.events.length === 1")
        self.assertIn('合成演示',page.locator('#demo-banner').inner_text())
        self.assertIn('3600',page.locator('#source-health').inner_text())
        page.locator('[data-event-id="e1"]').first.click()
        self.assertIn('p1',page.locator('#selection').inner_text())
        self.assertEqual(page.locator('#candidates tbody tr').first.locator('td').nth(1).inner_text(),'B')
        self.assertIn('缺账户平仓回报',page.locator('#position-detail').inner_text())
        page.select_option('#ledger', 'model')
        self.assertIn('模型账净收益', page.locator('#position-detail').inner_text())
        self.assertNotIn('账户账实际净收益', page.locator('#position-detail').inner_text())
        page.select_option('#ledger', 'account')
        self.assertIn('账户账实际净收益', page.locator('#position-detail').inner_text())
        page.select_option('#window', '3600')
        page.locator('#history-load').click()
        page.wait_for_function("window.MarketReview && window.MarketReview.state.selected && window.MarketReview.state.selected.position_id === 'p1'")
        page.select_option('#coordinate','spread')
        self.assertEqual(page.evaluate('MarketReview.state.chartData[0].close'),0)
        self.assertEqual(page.evaluate('MarketReview.state.chartData[0].low'),-2)
        self.assertIn('p2',page.locator('#unresolved').inner_text())
        self.assertFalse(page.evaluate('MarketReview.state.follow'))
        before=len(requests)
        page.evaluate('MarketReview.refresh()')
        self.assertGreater(len(requests),before)
        self.assertFalse(page.evaluate('MarketReview.state.follow'))
        page.evaluate('async () => {for(let i=0;i<100;i++) await MarketReview.refresh();}')
        self.assertEqual(page.evaluate('MarketReview.state.chartCount'),2)
        self.assertEqual(page.evaluate('MarketReview.state.timerCount'),1)
        self.assertEqual(page.evaluate('document.documentElement.scrollWidth <= innerWidth'),True)
        self.assertEqual(errors,[])
        page.screenshot(path='/tmp/v6-market-mobile.png',full_page=True)
        page.close()

    def review_page(self):
        page = self.browser.new_page(viewport={'width':390,'height':844})
        snapshot = {'run_id':'run-a','sequence':1,'quotes':[{'contract':'SHFE.au2612','source_time':'2026-09-07T01:00:00.125Z','last_price':960,'international_price':None}], 'bands':[{'contract':'SHFE.au2612','source_time':'2026-09-07T01:00:00Z','center':959}], 'synthetic':True}
        transport = {'snapshot':snapshot,'requests':[], 'status':200, 'more':False}
        def route(r):
            url=r.request.url;transport['requests'].append(url)
            if '/auth/me' in url:data={'username':'test'}
            elif '/latest' in url:data=transport['snapshot']
            elif '/series' in url:data={'quotes':transport['snapshot']['quotes'],'bands':transport['snapshot']['bands'],'next_after':'opaque-cursor','has_more':transport['more']}
            elif '/events' in url:data={'events':[{'run_id':transport['snapshot']['run_id'],'event_id':transport['snapshot']['run_id']+'-event','sequence':1,'event_type':'TEST','occurred_at':'2026-09-07T01:00:00Z'}], 'next_sequence':1}
            elif '/comparison' in url:data={'available':False}
            else:data={}
            r.fulfill(status=transport['status'],content_type='application/json',body=json.dumps(data))
        page.route('**/api/**',route)
        page.goto(f'http://127.0.0.1:{self.http.server_port}/market/')
        page.wait_for_function('window.MarketReview && MarketReview.state.selected && !MarketReview.state.loading')
        page.evaluate('clearInterval(MarketReview.state.timer)')
        return page,transport

    def test_missing_international_and_millisecond_band(self):
        page,_=self.review_page()
        page.select_option('#coordinate','spread')
        self.assertIsNone(page.evaluate('MarketReview.state.chartData[0].close'))
        page.select_option('#coordinate','deviation')
        self.assertEqual(page.evaluate('MarketReview.state.chartData[0].close'),1)
        page.close()

    def test_run_change_and_restricted_cache_erasure(self):
        page,t=self.review_page()
        t['snapshot']={**t['snapshot'],'run_id':'run-b'}
        page.evaluate('MarketReview.refresh()')
        self.assertEqual(page.evaluate('MarketReview.state.events.map(e => e.run_id)'),['run-b'])
        self.assertEqual(page.evaluate('MarketReview.state.selected.run_id'),'run-b')
        for status in (401,403):
            t['status']=status
            page.evaluate('MarketReview.refresh()')
            self.assertEqual(page.evaluate('MarketReview.state.quotes.length'),0)
            self.assertEqual(page.evaluate('MarketReview.state.chartData.length'),0)
            self.assertIsNone(page.evaluate('MarketReview.state.snapshot'))
        self.assertNotIn('run-b-event',page.locator('body').inner_text())
        t['status']=200
        page.evaluate('MarketReview.refresh()')
        page.evaluate('logout()')
        self.assertIsNone(page.evaluate('MarketReview.state.snapshot'))
        self.assertEqual(page.evaluate('MarketReview.state.chartData.length'),0)
        page.close()

    def test_history_pagination_keeps_window_and_refresh_is_incremental(self):
        from urllib.parse import urlparse,parse_qs
        page,t=self.review_page()
        t['requests'].clear()
        page.evaluate('MarketReview.refresh()')
        series=[u for u in t['requests'] if '/series?' in u]
        self.assertTrue(series)
        self.assertEqual(parse_qs(urlparse(series[-1]).query).get('after'),['opaque-cursor'])
        t['more']=True
        page.locator('#history-load').click()
        page.wait_for_function('MarketReview.state.seriesHasMore')
        first=[u for u in t['requests'] if '/series?' in u][-1]
        page.evaluate("MarketReview.state.quotes.push({contract:'SHFE.au2612',source_time:'2026-09-07T02:00:00Z',last_price:970})")
        page.locator('#history-more').click()
        page.wait_for_function('!MarketReview.state.seriesLoading')
        second=[u for u in t['requests'] if '/series?' in u][-1]
        a,b=(parse_qs(urlparse(u).query) for u in (first,second))
        self.assertEqual((a['start'],a['end']),(b['start'],b['end']))
        self.assertFalse(page.evaluate('MarketReview.state.follow'))
        self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        page.close()
