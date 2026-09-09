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

    def test_a_overview_eight_contracts_alert_and_required_viewports(self):
        page=self.browser.new_page(viewport={'width':390,'height':844})
        contracts=[f'SHFE.au{2612+i}' for i in range(8)]
        quotes=[]; bands=[]
        for index,contract in enumerate(contracts):
            stamp=f'2026-09-07T01:00:0{index}Z'
            quotes.append({'contract':contract,'source_time':stamp,'last_price':960+index,
                'sources':{'xau':{'source_time':stamp,'price':2650+index,'age_seconds':0},'fx':{'source_time':stamp,'price':7.1,'age_seconds':0}},
                'ohlc':{'open':960+index,'high':961+index,'low':959+index,'close':960+index},
                'orders':[{'side':'sell','price':965+index,'status':'EFFECTIVE｜有效挂单'},
                          {'side':'buy','price':955+index,'status':'EFFECTIVE｜有效挂单'}],
                'order_history':[{'captured_at':stamp,'orders':[]}]} )
            bands.append({'contract':contract,'source_time':stamp,'center':960+index,'lower':959+index,'upper':961+index})
        snapshot={'schema_version':'market-review.v1','model_version':'V6.0','run_id':'overview-run','sequence':8,'synthetic':True,
            'published_at':'2026-09-07T01:00:08Z','quotes':quotes,'bands':bands}
        event={'event_id':'target-fill-overview','run_id':'overview-run','sequence':1,'position_id':'p1',
            'event_type':'TARGET_FILL_CONFIRMED｜目标腿成交确认','occurred_at':'2026-09-07T01:00:01Z',
            'payload':{'symbol':contracts[2]}}
        def route(r):
            url=r.request.url
            if '/auth/me' in url:data={'username':'overview-user'}
            elif '/latest' in url:data=snapshot
            elif '/series' in url:data={'quotes':quotes,'bands':bands,'has_more':False}
            elif '/events' in url:data={'events':[event],'next_sequence':1,'has_more':False,'missing_sequence_ranges':[]}
            elif '/alerts?' in url:data={'alerts':[event],'read_event_ids':[],'has_more':False}
            elif '/positions/' in url:data={'position_id':'p1','run_id':'overview-run','account':{'status':'UNRESOLVED'},'reasons':[]}
            elif '/comparison' in url:data={'available':False}
            else:data={}
            r.fulfill(status=200,content_type='application/json',body=json.dumps(data))
        page.route('**/api/**',route)
        page.goto(f'http://127.0.0.1:{self.http.server_port}/market/')
        page.wait_for_function("document.querySelectorAll('#market-overview .market-card').length === 8")
        page.wait_for_function("!MarketReview.state.loading")
        self.assertEqual(page.locator('#market-overview .market-card').count(),8)
        self.assertEqual(page.locator('.market-card.alerted').count(),0)
        self.assertIn('历史未读 1 笔',page.locator('#alert-center').inner_text())
        event.update(event_id='target-fill-new',sequence=2)
        page.evaluate('MarketReview.refresh()')
        self.assertEqual(page.locator('.market-card.alerted').count(),1)
        self.assertEqual(page.locator('#main-price-tags span').count(),5)
        self.assertIn('窗口高/低',page.locator('#international-quality').inner_text())
        page.locator('#volume-toggle').uncheck()
        self.assertTrue(page.locator('#volume-chart').evaluate("node => node.classList.contains('hidden')"))
        for width in (390,1366,1600,1920):
            page.set_viewport_size({'width':width,'height':900})
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),width)
            page.screenshot(path=f'/tmp/v6-market-{width}.png',full_page=True)
        page.close()

    def review_page(self, stop_timer=True, initial=None):
        page = self.browser.new_page(viewport={'width':390,'height':844})
        snapshot = {'run_id':'run-a','sequence':1,'quotes':[{'contract':'SHFE.au2612','source_time':'2026-09-07T01:00:00.125Z','last_price':960,'international_price':None}], 'bands':[{'contract':'SHFE.au2612','source_time':'2026-09-07T01:00:00Z','center':959}], 'synthetic':True}
        transport = {'snapshot':snapshot,'requests':[], 'status':200, 'more':False,'events':None,'alerts':[], 'read':set(),'positions':{},'alert_page_size':2}
        transport.update(initial or {})
        def route(r):
            url=r.request.url;transport['requests'].append(url)
            if '/auth/me' in url:data={'username':'test'}
            elif '/latest' in url:data=transport['snapshot']
            elif '/series' in url:data={'quotes':transport.get('series_quotes',transport['snapshot']['quotes']),'bands':transport['snapshot']['bands'],'next_after':'opaque-cursor','has_more':transport['more']}
            elif '/events' in url:data={'events':transport['events'] if transport['events'] is not None else [{'run_id':transport['snapshot']['run_id'],'event_id':transport['snapshot']['run_id']+'-event','sequence':1,'event_type':'TEST','occurred_at':'2026-09-07T01:00:00Z'}], 'next_sequence':1,**transport.get('event_meta',{})}
            elif '/alerts/' in url:
                transport['read'].add(url.split('/alerts/')[1].split('/')[0]);data={}
            elif '/alerts?' in url:
                from urllib.parse import parse_qs,urlparse
                after=int(parse_qs(urlparse(url).query)['after_sequence'][0])
                pending=[e for e in transport['alerts'] if e['sequence']>after]
                chunk=pending[:transport['alert_page_size']]
                data={'alerts':chunk,'read_event_ids':list(transport['read']),'has_more':len(pending)>len(chunk),'next_sequence':chunk[-1]['sequence'] if chunk else after}
            elif '/positions/' in url:data=transport['positions'].get(url.split('/positions/')[1].split('?')[0],{})
            elif '/comparison' in url:data={'available':False}
            else:data={}
            r.fulfill(status=transport['status'],content_type='application/json',body=json.dumps(data))
        page.route('**/api/**',route)
        page.goto(f'http://127.0.0.1:{self.http.server_port}/market/')
        page.wait_for_function('window.MarketReview && MarketReview.state.selected && !MarketReview.state.loading')
        if stop_timer:
            page.evaluate('clearInterval(MarketReview.state.timer)')
        return page,transport

    def test_missing_international_and_millisecond_band(self):
        page,_=self.review_page()
        page.select_option('#coordinate','spread')
        self.assertIsNone(page.evaluate('MarketReview.state.chartData[0].close'))
        page.select_option('#coordinate','deviation')
        self.assertEqual(page.evaluate('MarketReview.state.chartData[0].close'),1)
        page.close()

    def three_point_page(self, stop_timer=True):
        page, transport = self.review_page(stop_timer=stop_timer)
        quotes = [{
            'contract':'SHFE.au2612', 'source_time':f'2026-09-07T01:00:0{i}Z',
            'observed_at':f'2026-09-07T01:00:0{i}Z', 'trading_day':'2026-09-07',
            'last_price':960+i, 'volume':volume,
            'sources':{'xau':{'source_time':f'2026-09-07T01:00:0{i}Z','price':2650+i,'age_seconds':0}},
            'orders':[{'side':'buy','price':955+i,'status':'EFFECTIVE｜有效挂单'},
                      {'side':'sell','price':965+i,'status':'EFFECTIVE｜有效挂单'}],
        } for i, volume in enumerate((100,103,109))]
        bands = [{'contract':q['contract'],'source_time':q['source_time'], 'observed_at':q['observed_at'],
                  'center':960,'lower':959,'upper':961} for q in quotes]
        transport['snapshot'] = {**transport['snapshot'],'quotes':quotes,'bands':bands}
        page.evaluate('MarketReview.refresh()')
        return page, transport

    def test_three_observations_are_three_mini_chart_points(self):
        page,_=self.three_point_page()
        self.assertEqual(page.evaluate("MarketReview.state.miniCharts.get('SHFE.au2612').series.data().length"),3)
        page.close()

    def test_chart_canvas_is_inside_visible_container_not_global_table_minwidth(self):
        page,_=self.three_point_page()
        for width in (390,1600):
            page.set_viewport_size({'width':width,'height':1000})
            page.wait_for_timeout(100)
            self.assertTrue(page.locator('[data-mini-contract]').first.evaluate("node=>{const parent=node.getBoundingClientRect();return [...node.querySelectorAll('canvas')].filter(c=>c.getBoundingClientRect().width>0).every(c=>{const r=c.getBoundingClientRect();return r.left>=parent.left && r.right<=parent.right+1})}"))
            self.assertTrue(page.locator('#international-chart').evaluate("node=>{const parent=node.getBoundingClientRect();return [...node.querySelectorAll('canvas')].filter(c=>c.getBoundingClientRect().width>0).every(c=>{const r=c.getBoundingClientRect();return r.left>=parent.left && r.right<=parent.right+1})}"))
        page.close()

    def test_refresh_retains_connected_chart_container(self):
        page,_=self.three_point_page()
        page.evaluate('MarketReview.refresh()')
        self.assertTrue(page.evaluate("MarketReview.state.miniCharts.get('SHFE.au2612').chart.chartElement().isConnected"))
        page.close()

    def test_volume_is_bucket_lots_not_cumulative(self):
        page,_=self.three_point_page()
        data=page.evaluate("document.getElementById('volume-chart').__reviewChart.series.data().filter(r=>r.value!=null).map(r=>r.value)")
        self.assertEqual(data,[3,6])
        page.close()

    def test_cursor_prevents_future_price_and_events(self):
        page,_=self.three_point_page()
        page.locator('#step-back').click()
        self.assertTrue(page.evaluate("MarketReview.state.chartData.every(r=>Date.parse(r.time)<=Date.parse('2026-09-07T01:00:01Z'))"))
        self.assertEqual(page.locator('#main-price-tags .current').inner_text(),'961.00')
        self.assertEqual(page.evaluate("MarketReview.state.miniCharts.get('SHFE.au2612').series.data().length"),2)
        page.close()

    def test_observation_clock_volume_revision_reset_and_order_cancellation(self):
        page,t=self.three_point_page()
        q=t['snapshot']['quotes']
        q[0]['volume_delta']=None
        q[1]['volume_delta']=3
        q[2]['volume_delta']=6
        revision={**q[2],'observed_at':'2026-09-07T01:00:02.500Z','volume_delta':9,
                  'orders':[{'side':'buy','price':957,'status':'CANCELLED｜已撤单'}]}
        reset={**q[2],'source_time':'2026-09-07T01:00:03Z','observed_at':'2026-09-07T01:00:03Z',
               'volume':2,'volume_delta':None,'volume_delta_reason':'RESET｜累计量重置','last_price':970}
        t['snapshot']['quotes']=[*q,revision,reset]
        page.evaluate('MarketReview.refresh()')
        self.assertEqual(page.evaluate("document.getElementById('volume-chart').__reviewChart.series.data().filter(r=>r.value!=null).map(r=>r.value)"),[3,9])
        page.evaluate("MarketReview.state.cursor='2026-09-07T01:00:02.500Z';MarketReview.state.follow=false;MarketReview.renderView()")
        self.assertEqual(page.locator('#main-price-tags .buy').inner_text(),'—')
        data=page.evaluate("MarketReview.state.miniCharts.get('SHFE.au2612').orderRows.buy")
        self.assertIsNone(data[-1].get('value'),f'{data}; {page.locator("#review-status").inner_text()}')
        self.assertEqual(page.evaluate("MarketReview.state.chartData.at(-1).quote.source_time"),'2026-09-07T01:00:02Z')
        self.assertEqual(page.evaluate("MarketReview.state.chartData.at(-1).time"),'2026-09-07T01:00:02.500Z')
        page.close()

    def test_repeated_order_history_uses_original_observation_base(self):
        page,t=self.three_point_page()
        quotes=t['snapshot']['quotes']
        for i,q in enumerate(quotes):
            q['international_price']=950+i*10
            q['order_history']=[{'observed_at':quotes[0]['observed_at'], 'orders':quotes[0]['orders']}]
        page.evaluate('MarketReview.refresh()')
        page.select_option('#coordinate','spread')
        data=page.evaluate("document.getElementById('main-chart').__reviewChart.layers.buy.data()")
        self.assertEqual(data[0]['value'],5)
        self.assertEqual(data[-1]['value'],-13)
        page.close()

    def test_five_prices_reorder_at_true_price_and_avoid_collisions(self):
        page,t=self.three_point_page()
        t['snapshot']['quotes'][-1]['last_price']=970
        page.evaluate('MarketReview.refresh()')
        labels=page.locator('.market-card .five-price span').evaluate_all("nodes=>nodes.map(n=>({kind:n.className,price:Number(n.dataset.price),y:parseFloat(n.style.top),anchor:Number(n.dataset.anchor)}))")
        self.assertEqual(labels[0]['kind'],'current')
        self.assertEqual([v['price'] for v in labels],[970,967,961,959,957])
        self.assertTrue(all(b['y']-a['y']>=16 for a,b in zip(labels,labels[1:])))
        t['snapshot']['quotes'][-1]['last_price']=961
        page.evaluate('MarketReview.refresh()')
        equal=page.locator('.market-card .five-price span').evaluate_all("nodes=>nodes.filter(n=>Number(n.dataset.price)===961).map(n=>({y:parseFloat(n.style.top),anchor:Number(n.dataset.anchor)}))")
        self.assertEqual(len(equal),2)
        self.assertAlmostEqual(equal[0]['anchor'],equal[1]['anchor'])
        self.assertGreaterEqual(abs(equal[0]['y']-equal[1]['y']),16)
        page.close()

    def test_international_independent_clock_and_subsecond_extremes(self):
        page,t=self.three_point_page()
        q=t['snapshot']['quotes'][-1]
        t['snapshot']['quotes'] += [{**q,'observed_at':f'2026-09-07T01:00:02.{ms}Z',
            'sources':{'xau':{'source_time':f'2026-09-07T01:00:02.{ms}Z','price':price,'age_seconds':0}},'volume_delta':0}
            for ms,price in ((100,2700),(900,2600))]
        page.evaluate('MarketReview.refresh()')
        data=page.evaluate("document.getElementById('international-chart').__reviewChart.series.data()")
        self.assertEqual([v['value'] for v in data][-2:],[2700,2600])
        self.assertIn('2700.00 / 2600.00',page.locator('#international-quality').inner_text())
        page.evaluate("MarketReview.state.cursor='2026-09-07T01:00:02.500Z';MarketReview.renderView()")
        self.assertEqual(page.locator('#international-value').inner_text(),'2700.00')
        self.assertNotIn('2600',page.locator('#international-quality').inner_text())
        page.close()

    def test_cursor_updates_legacy_quotes_and_missing_international_is_unknown(self):
        page,t=self.three_point_page()
        page.locator('#step-back').click()
        self.assertEqual(page.locator('#rows tr').first.locator('td').nth(4).inner_text(),'961.00')
        page.locator('#return-live').click()
        page.wait_for_function('!MarketReview.state.loading')
        quote=t['snapshot']['quotes'][-1]
        t['snapshot']['quotes'].append({**quote,'observed_at':'2026-09-07T01:00:03Z','sources':{'xau':None}})
        page.evaluate('MarketReview.refresh()')
        self.assertEqual(page.locator('#international-value').inner_text(),'—')
        self.assertIn('缺失',page.locator('#international-quality').inner_text())
        self.assertIsNone(page.evaluate("document.getElementById('international-chart').__reviewChart.plotRows.at(-1).value ?? null"))
        page.close()

    def test_bidirectional_preview_frozen_decision_and_candidate_observation(self):
        page,t=self.three_point_page()
        for q in t['snapshot']['quotes']:
            q['hedge_previews']=[{'target_symbol':q['contract'],'target_side':side,'as_of':q['observed_at'],
                'assumed_price_cny_per_g':955 if side=='buy' else 965,'quantity_lots':1,'status':'READY｜只读预览已就绪',
                'candidates':[{'contract':'SHFE.au2702','rank':7,'eligible':True,'selected':True,'expected_cost_cny_per_pair':12},
                              {'contract':'SHFE.au2704','rank':2,'eligible':False,'reason_code':'STALE｜报价过期'}],
                'selected_symbol':'SHFE.au2702'} for side in ('buy','sell')]
        event={'run_id':'run-a','event_id':'decision','sequence':3,'position_id':'p1','occurred_at':'2026-09-07T01:00:01Z','event_type':'CANDIDATE_SNAPSHOT',
               'payload':{'decision_id':'frozen-1','target_symbol':'SHFE.au2612','selected_symbol':'SHFE.au2704',
                          'candidates':[{'contract':'SHFE.au2704','rank':1,'selected':True,'eligible':True,'expected_cost_cny_per_pair':45}]}}
        t['events']=[event]
        t['positions']['p1']={'position_id':'p1','model':{'net_pnl_cny':950},'account':{'actual_net_cny':None},'events':[event],'decisions':[event]}
        page.evaluate('MarketReview.refresh()')
        page.select_option('#candidate-mode','preview')
        self.assertEqual(page.locator('#candidates tbody tr').first.locator('td').first.inner_text(),'7 / —')
        self.assertIn('955.00',page.locator('#preview-status').inner_text())
        page.select_option('#preview-side','sell')
        self.assertIn('965.00',page.locator('#preview-status').inner_text())
        page.locator('[data-candidate-contract="SHFE.au2704"]').click()
        self.assertIn('SHFE.au2704',page.locator('#hedge-focus-label').inner_text())
        self.assertEqual(t['snapshot']['quotes'][-1]['hedge_previews'][1]['selected_symbol'],'SHFE.au2702')
        page.locator('#actual-candidate').click()
        self.assertIn('SHFE.au2702',page.locator('#hedge-focus-label').inner_text())
        page.select_option('#candidate-mode','decision')
        page.locator('[data-event-id="decision"]').click()
        self.assertIn('45.00',page.locator('#candidates tbody').inner_text())
        self.assertIn('冻结历史决策',page.locator('#preview-status').inner_text())
        page.locator('#step-back').click()
        self.assertEqual(page.locator('#candidates tbody tr').count(),0)
        self.assertNotIn('decision',page.locator('#raw-event').text_content())
        page.close()

    def test_paused_alert_pagination_read_and_multi_position_lifecycle(self):
        page,t=self.three_point_page()
        page.locator('#step-back').click()
        for i in range(1,4):
            event={'run_id':'run-a','event_id':f'fill-{i}','position_id':f'p{i}','sequence':i+2,
                'event_type':'TARGET_FILL_CONFIRMED｜目标腿成交确认','occurred_at':'2026-09-07T01:00:02Z',
                'payload':{'symbol':'SHFE.au2612','raw_ticks':[{'source_time':'2026-09-07T01:00:02Z','last_price':952+i}]}}
            t['alerts'].append(event)
            t['positions'][f'p{i}']={'position_id':f'p{i}','events':[event],'model':{'net_pnl_cny':950},'account':{'actual_net_cny':None,'status':'UNRESOLVED'}}
        t['events']=[*t['alerts'],{**t['alerts'][0],'event_id':'hedge-fill','event_type':'HEDGE_FILL','sequence':9}]
        t['snapshot']['unresolved_positions']=[{'position_id':'p1'}]
        page.evaluate('MarketReview.refresh()')
        page.locator('#alerts-more').click()
        page.wait_for_function('MarketReview.state.unreadAlertIds.size===3')
        self.assertEqual(page.locator('[data-open-alert]').count(),3)
        self.assertEqual(page.locator('#events [data-event-id^="fill-"]').count(),0)
        self.assertEqual(page.locator('#main-price-tags .current').inner_text(),'961.00')
        self.assertIn('3 笔未读',page.locator('.market-card h3').inner_text())
        page.locator('[data-open-alert="fill-2"]').click()
        page.wait_for_function("MarketReview.state.position?.position_id==='p2'")
        self.assertIn('954.00',page.locator('#raw-evidence').inner_text())
        self.assertIn('fill-2',page.locator('#raw-event').text_content())
        self.assertEqual(page.locator('[data-position]').count(),3)
        self.assertEqual(page.locator('[data-stage="fill-2"]').count(),1)
        page.locator('[data-read-alert="fill-2"]').click()
        page.wait_for_function('MarketReview.state.unreadAlertIds.size===2')
        page.evaluate('MarketReview.refresh()')
        self.assertEqual(page.locator('[data-open-alert]').count(),2)
        self.assertIn('p1',page.locator('#unresolved').inner_text())
        self.assertIn('UNRESOLVED',page.locator('#position-detail').inner_text())
        page.close()

    def test_alert_stream_has_incremental_requests_and_bounded_cache_dom(self):
        page,t=self.three_point_page()
        t['alert_page_size']=500
        t['alerts']=[{'run_id':'run-a','event_id':f'bulk-{i}','position_id':f'p-{i}','sequence':i+2,
            'event_type':'TARGET_FILL_CONFIRMED','occurred_at':'2026-09-07T01:00:02Z','payload':{'symbol':'SHFE.au2612'}} for i in range(6001)]
        t['requests'].clear()
        page.evaluate('MarketReview.refresh()')
        self.assertEqual(len([url for url in t['requests'] if '/alerts?' in url]),1)
        self.assertEqual(page.locator('[data-open-alert]').count(),100)
        self.assertIn('已加载',page.locator('#alert-center').inner_text())
        t['requests'].clear()
        page.evaluate('async()=>{for(let i=0;i<12;i++) await MarketReview.refresh()}')
        requests=[url for url in t['requests'] if '/alerts?' in url]
        self.assertEqual(len(requests),12)
        self.assertNotIn('after_sequence=0&',requests[0])
        self.assertEqual(page.evaluate('MarketReview.state.alertEvents.length'),5000)
        self.assertEqual(page.locator('[data-open-alert]').count(),100)
        self.assertIn('截断',page.locator('#alert-center').inner_text())
        page.close()

    def test_initial_history_unread_is_preserved_without_live_red_alerts(self):
        historical = [{'run_id':'run-a','event_id':f'history-{i}','position_id':f'p-{i}',
            'sequence':i,'event_type':'TARGET_FILL_CONFIRMED',
            'occurred_at':'2026-09-07T01:00:00Z','payload':{'symbol':'SHFE.au2612'}}
            for i in range(1,6002)]
        page,t = self.review_page(initial={'events':historical[:500], 'alerts':historical,
            'alert_page_size':500,'event_meta':{'has_more':True,'next_sequence':500,
                'event_sequence_high_watermark':6001}})
        self.assertEqual(page.evaluate('MarketReview.state.unreadAlertIds.size'),500)
        self.assertEqual(page.locator('.market-card.alerted').count(),0)
        self.assertEqual(page.locator('.event-item.target-alert').count(),0)
        self.assertIn('历史未读 500 笔',page.locator('#alert-center').inner_text())
        self.assertNotIn('🔴',page.locator('#alert-center').inner_text())
        self.assertEqual(page.evaluate('MarketReview.state.alertBaseline'),6001)
        page.locator('#step-back').click()
        page.evaluate('async()=>{for(let i=0;i<12;i++) await MarketReview.refresh()}')
        self.assertEqual(page.locator('.market-card.alerted').count(),0)
        new = {**historical[0],'event_id':'new-6002','sequence':6002}
        t['alerts'].append(new)
        t['events']=[new]
        t['event_meta']={'has_more':False,'next_sequence':6002,'event_sequence_high_watermark':6002}
        page.evaluate('MarketReview.refresh()')
        self.assertEqual(page.locator('.market-card.alerted').count(),1)
        self.assertIn('本次新增未读 1 笔',page.locator('#alert-center').inner_text())
        self.assertEqual(page.locator('[data-open-alert="new-6002"]').count(),1)
        page.locator('[data-read-alert="new-6002"]').click()
        page.wait_for_function("!MarketReview.state.unreadAlertIds.has('new-6002')")
        page.evaluate('MarketReview.refresh()')
        self.assertEqual(page.locator('.market-card.alerted').count(),0)
        page.close()

    def test_all_restricted_layers_are_empty_on_auth_expiry(self):
        page,t=self.three_point_page()
        t['status']=403
        page.evaluate('MarketReview.refresh()')
        self.assertEqual(page.locator('.market-card').count(),0)
        self.assertEqual(page.evaluate('MarketReview.state.miniCharts.size'),0)
        self.assertEqual(page.locator('#international-value').inner_text(),'—')
        self.assertEqual(page.evaluate("document.getElementById('international-chart').__reviewChart.series.data().length"),0)
        self.assertTrue(page.evaluate("[...MarketReview.state.focusCharts.values()].every(c=>c.series.data().length===0 && Object.values(c.layers).every(s=>s.data().length===0))"))
        self.assertNotIn('960',page.locator('#main-price-tags').inner_text())
        self.assertEqual(page.locator('#alert-center [data-open-alert]').count(),0)
        page.close()

    def test_model_result_does_not_close_account_and_history_keeps_known_fills(self):
        page,t=self.three_point_page()
        def event(kind,sec,seq,payload):
            return {'run_id':'run-a','event_id':f'p-open-{seq}','position_id':'p-open','sequence':seq,
                    'trading_day':'2026-09-07','order_id':'order-synthetic','trade_id':f'trade-{seq}',
                    'event_type':kind,'occurred_at':f'2026-09-07T01:00:0{sec}Z','payload':payload}
        opened=event('ACCOUNT_TRADE',0,2,{'leg':'target','symbol':'SHFE.au2612','direction':'BUY','offset':'OPEN','volume':1,'price':960,'multiplier':1000,'account_id':'synthetic','trade_id':'t1'})
        result=event('MODEL_RESULT',1,3,{'net_pnl_cny':950})
        rejected=event('HEDGE_REJECT',2,4,{'reason':'未来拒绝理由不可提前出现'})
        t['events']=[opened,result,rejected]
        t['positions']['p-open']={'position_id':'p-open','model':{'net_pnl_cny':950},
            'account':{'status':'UNRESOLVED','legs':{'target':{'opened_volume':1,'closed_volume':0,'remaining_volume':1,'fills':[opened]}}},
            'events':t['events'],'reasons':[{'event_id':rejected['event_id'],'reason':rejected['payload']['reason']}]}
        page.evaluate('MarketReview.refresh()')
        page.locator('[data-event-id="p-open-3"]').click()
        page.wait_for_function("MarketReview.state.position?.position_id==='p-open'")
        page.select_option('#position-filter','closed')
        self.assertEqual(page.locator('[data-position="p-open"]').count(),0)
        page.select_option('#position-filter','unresolved')
        self.assertEqual(page.locator('[data-position="p-open"]').count(),1)
        text=page.locator('#position-detail').text_content()
        self.assertNotIn('未来拒绝理由',text)
        self.assertIn('已开 1 / 已平 0 / 剩余 1 手',text)
        self.assertIn('950.00',text)
        page.locator('#step-back').click()
        self.assertNotIn('950.00',page.locator('#position-detail').text_content())
        self.assertIn('已开 1 / 已平 0 / 剩余 1 手',page.locator('#position-detail').text_content())
        page.close()

    def test_late_source_remains_evidence_without_rolling_back_current_price(self):
        page,t=self.three_point_page()
        current={**t['snapshot']['quotes'][1],'observed_at':'2026-09-07T01:00:01.010Z'}
        late={**current,'source_time':'2026-09-07T01:00:00.500Z','observed_at':'2026-09-07T01:00:01.500Z','last_price':955}
        t['snapshot']['quotes']=[current];t['series_quotes']=[current,late]
        page.evaluate('MarketReview.state.quotes=[];MarketReview.refresh()')
        self.assertEqual(page.locator('#main-price-tags .current').inner_text(),'961.00')
        self.assertEqual(page.locator('#rows tr').first.locator('td').nth(4).inner_text(),'961.00')
        self.assertEqual(page.evaluate('MarketReview.state.chartData.at(-1).close'),961)
        self.assertTrue(page.evaluate('MarketReview.state.quotes.some(row=>row.last_price===955)'))
        self.assertIn('迟到',page.locator('#source-health').inner_text())
        page.evaluate("MarketReview.state.cursor='2026-09-07T01:00:01.020Z';MarketReview.renderView()")
        self.assertEqual(page.locator('#main-price-tags .current').inner_text(),'961.00')
        page.close()

    def test_history_account_dedup_keeps_trading_days_and_rejects_missing_ids(self):
        page,_=self.three_point_page()
        events=[]
        for i,day in enumerate(('2026-09-06','2026-09-07')):
            events.append({'event_id':f'day-{i}','trading_day':day,'order_id':'same-order','trade_id':'same-trade','event_type':'ACCOUNT_TRADE',
                'occurred_at':f'{day}T01:00:00Z','payload':{'account_id':'synthetic','leg':'target','offset':'OPEN','direction':'BUY','price':960,'volume':1,'multiplier':1000}})
        events.append({**events[1],'event_id':'bad-id','trade_id':None,'payload':{**events[1]['payload'],'volume':9}})
        events.append({'event_id':'future','event_type':'MODEL_RESULT','occurred_at':'2026-09-07T01:00:02Z','payload':{'net_pnl_cny':950}})
        position={'position_id':'cross-day','events':events,'account':{'status':'UNRESOLVED'},'model':{'net_pnl_cny':950}}
        page.evaluate("position=>{MarketReview.state.position=position;MarketReview.state.cursor='2026-09-07T01:00:01Z';MarketReview.renderView()}",position)
        text=page.locator('#position-detail').text_content()
        self.assertIn('已开 2 / 已平 0 / 剩余 2 手',text)
        self.assertIn('账户成交标识或腿缺失',text)
        page.close()

    def test_selected_position_updates_on_new_events_without_reselect_or_polling(self):
        page,t=self.three_point_page()
        opened={'run_id':'run-a','event_id':'tracked-open','position_id':'tracked','sequence':2,
            'event_type':'TARGET_FILL_CONFIRMED','occurred_at':'2026-09-07T01:00:00Z','payload':{'symbol':'SHFE.au2612'}}
        t['events']=[opened]
        t['positions']['tracked']={'position_id':'tracked','events':[opened],'account':{'status':'UNRESOLVED'},'model':{}}
        page.evaluate('MarketReview.refresh()')
        page.evaluate('(event)=>MarketReview.selectEvent(event,false)',opened)
        page.evaluate("MarketReview.state.observedCandidateSymbol='SHFE.au2702'")
        closed={**opened,'event_id':'tracked-close','sequence':3,'event_type':'MODEL_RESULT','occurred_at':'2026-09-07T01:00:02Z','payload':{'net_pnl_cny':950}}
        t['events']=[opened,closed]
        t['positions']['tracked']={'position_id':'tracked','events':[opened,closed],'account':{'status':'CLOSED'},'model':{'net_pnl_cny':950}}
        t['requests'].clear()
        page.evaluate('MarketReview.refresh()')
        self.assertIn('950.00',page.locator('#position-detail').text_content())
        self.assertEqual(page.locator('[data-stage="tracked-close"]').count(),1)
        self.assertEqual(page.evaluate('MarketReview.state.selected.event_id'),'tracked-open')
        self.assertEqual(page.evaluate('MarketReview.state.observedCandidateSymbol'),'SHFE.au2702')
        self.assertIsNone(page.evaluate('MarketReview.state.cursor'))
        page.evaluate('MarketReview.refresh()')
        self.assertEqual(len([url for url in t['requests'] if '/positions/' in url]),1)
        page.close()

    def test_event_gap_truncation_is_not_presented_as_complete(self):
        page,t=self.three_point_page()
        t['event_meta']={'missing_sequence_ranges':[[1,3]],'missing_sequence_ranges_truncated':True,'event_sequence_high_watermark':4001}
        page.evaluate('MarketReview.refresh()')
        self.assertIn('缺口列表截断',page.locator('#event-gaps').inner_text())
        self.assertIn('4001',page.locator('#event-gaps').inner_text())
        page.close()

    def test_target_contract_stays_anchored_through_hedge_and_two_exits(self):
        page,t=self.three_point_page()
        q=t['snapshot']['quotes'][-1]
        t['snapshot']['quotes'].append({**q,'contract':'SHFE.au2702'})
        events=[]
        for i,(leg,offset) in enumerate((('target','OPEN'),('hedge','OPEN'),('target','CLOSE'),('hedge','CLOSE'))):
            events.append({'run_id':'run-a','event_id':f'leg-{i}','sequence':i+2,'position_id':'legs','event_type':'ACCOUNT_TRADE',
                'occurred_at':f'2026-09-07T01:00:0{min(i,2)}Z','payload':{'leg':leg,'offset':offset,'direction':'BUY','volume':1,
                'symbol':'SHFE.au2612' if leg=='target' else 'SHFE.au2702','price':960,'multiplier':1000}})
        t['events']=events;t['positions']['legs']={'position_id':'legs','events':events,'account':{'status':'CLOSED'}}
        page.evaluate('MarketReview.refresh()')
        for event in events:
            page.evaluate('(event)=>MarketReview.selectEvent(event)',event)
            self.assertEqual(page.locator('#contract').input_value(),'SHFE.au2612')
            self.assertIn('SHFE.au2612',page.locator('#target-focus-label').inner_text())
        self.assertIn('SHFE.au2702',page.locator('#hedge-focus-label').inner_text())
        page.close()

    def test_missing_band_breaks_every_coordinate_without_old_overlay(self):
        page,t=self.three_point_page()
        for quote in t['snapshot']['quotes']:quote['international_price']=950
        t['snapshot']['bands'][1].update(upper=None,lower=None,center=None,fit_available=False)
        page.evaluate('MarketReview.refresh()')
        for mode in ('price','spread','deviation'):
            page.select_option('#coordinate',mode)
            self.assertEqual(page.evaluate("Object.keys(document.getElementById('main-chart').__reviewChart.bandSeries || {}).length"),0)
            data=page.evaluate("document.getElementById('main-chart').__reviewChart.bandRows")
            self.assertIsNone(data[1]['upper'])
            self.assertIsNone(data[1]['lower'])
        page.close()

    def test_playback_real_timer_speed_and_return_live(self):
        page,_=self.three_point_page(stop_timer=False)
        page.evaluate("MarketReview.state.cursor='2026-09-07T01:00:00Z';MarketReview.state.follow=false;MarketReview.renderView()")
        page.select_option('#play-speed','4')
        page.locator('#play').click()
        page.wait_for_function("MarketReview.state.cursor==='2026-09-07T01:00:02.000Z' && !MarketReview.state.playing")
        self.assertEqual(page.locator('#main-price-tags .current').inner_text(),'962.00')
        page.locator('#return-live').click()
        page.wait_for_function('MarketReview.state.follow && !MarketReview.state.loading')
        self.assertIsNone(page.evaluate('MarketReview.state.cursor'))
        self.assertEqual(page.locator('#cursor-label').inner_text(),'实时观察 · UTC+08:00｜北京时间')
        page.close()

    def test_eight_multi_point_charts_100_refreshes_switches_and_viewports(self):
        page,t=self.three_point_page()
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        quotes=t['snapshot']['quotes'];bands=t['snapshot']['bands']
        t['snapshot']['quotes']=[{**q,'contract':f'SHFE.au{2612+i}'} for i in range(8) for q in quotes]
        t['snapshot']['bands']=[{**b,'contract':f'SHFE.au{2612+i}'} for i in range(8) for b in bands]
        page.evaluate('MarketReview.refresh()')
        count=page.locator('canvas').count()
        nodes=page.locator('*').count()
        for width in (390,1366,1600,1920):
            page.set_viewport_size({'width':width,'height':1000})
            page.evaluate("async()=>{for(let i=0;i<25;i++){document.getElementById('contract').value='SHFE.au'+(2612+i%8);await MarketReview.refresh();}}")
            self.assertEqual(page.evaluate('MarketReview.state.miniCharts.size'),8)
            self.assertTrue(page.evaluate("[...MarketReview.state.miniCharts.values()].every(c=>c.chart.chartElement().isConnected && c.series.data().length===3 && c.layers.upper.data().length===3 && c.layers.buy.data().length===3 && c.volume.data().filter(r=>r.value!=null).length===2)"))
            self.assertEqual(page.locator('canvas').count(),count)
            self.assertLessEqual(page.locator('*').count(),nodes+4)
            self.assertEqual(page.evaluate('MarketReview.state.timerCount'),1)
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),width)
            if width>=1366:
                self.assertEqual(page.locator('#market-overview').evaluate("n=>getComputedStyle(n).gridTemplateColumns.split(' ').length"),4)
            page.screenshot(path=f'/tmp/v6-observation-complete-{width}.png',full_page=True)
        self.assertEqual(errors,[])
        self.assertTrue(any('symbol=*' in url for url in t['requests'] if '/series?' in url))
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
