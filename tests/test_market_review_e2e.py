"""Local real HTTP/auth/PostgreSQL/browser chain with synthetic source evidence.

Set V6_GOLD_SOURCE to the Gold checkout's src directory. No API route is mocked,
and this test never uses production credentials, data, account or infrastructure.
"""
from datetime import datetime, timedelta, timezone
import functools
import http.server
import importlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from test_market_review_storage import service  # noqa: F401


def test_source_to_authenticated_browser_and_durable_review(service, monkeypatch, tmp_path):
    source = os.getenv('V6_GOLD_SOURCE')
    if not source:
        pytest.skip('V6_GOLD_SOURCE not configured for cross-repository integration')
    monkeypatch.syspath_prepend(source)
    from gold_spread_monitor.market_review_runtime import ReviewSnapshotBuffer
    from gold_spread_monitor.live_sim.execution_ledger import ExecutionLedger
    from gold_spread_monitor.snapshot_publisher import AsyncReviewPublisher, push_snapshot
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from playwright.sync_api import expect, sync_playwright

    mod, connect = service
    core = importlib.import_module('app.core')
    market = importlib.import_module('app.routers.market_snapshot')
    auth = importlib.import_module('app.routers.auth_admin')
    monkeypatch.setenv('AUTH_TOKEN_SECRET', 'synthetic-e2e-signing-secret')
    monkeypatch.setenv('MARKET_SNAPSHOT_INGEST_TOKEN', 'synthetic-e2e-ingest')
    monkeypatch.setattr(core, '_conn', connect)
    with connect() as conn:
        conn.execute('CREATE TABLE users (id bigint PRIMARY KEY, token_version integer)')
        conn.execute('INSERT INTO users VALUES (7, 1)')
    token = core._encode_token(dict(uid=7, sub='synthetic-reviewer', tv=1,
        permissions=['market.read', 'market.annotate'], exp=int(time.time()) + 600))
    app = FastAPI()
    app.include_router(market.router, prefix='/api')
    app.include_router(auth.router, prefix='/api')
    client = TestClient(app, raise_server_exceptions=False)
    requests = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def dispatch(self):
            size = int(self.headers.get('Content-Length', 0))
            response = client.request(self.command, self.path,
                content=self.rfile.read(size) if size else None,
                headers={k: v for k, v in self.headers.items()
                         if k.lower() not in ('host', 'content-length')})
            requests.append((self.command, self.path, response.status_code))
            self.send_response(response.status_code)
            self.send_header('Content-Type', response.headers.get('content-type', 'application/json'))
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def do_GET(self):
            if self.path.startswith('/api/'):
                self.dispatch()
            else:
                super().do_GET()

        def do_POST(self):
            self.dispatch()

    root = Path(__file__).resolve().parents[1]
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(
        Handler, directory=str(root / 'services/public-web')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    ledger = ExecutionLedger(tmp_path / 'ledger')
    publisher = None
    try:
        at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=10)
        symbols = ['SHFE.au' + suffix for suffix in
                   ('2610', '2612', '2702', '2704', '2706', '2708', '2710', '2712')]
        buffer = ReviewSnapshotBuffer(run_id=ledger.run_id)
        band = SimpleNamespace(available=True, center_cny_per_g=960,
            lower_cny_per_g=958, upper_cny_per_g=962, references=(symbols[1],), basis_source='synthetic')
        for offset, price, volume in ((0, 960, 100), (1, 955, 103), (2, 962, 109)):
            stamp = at + timedelta(seconds=offset)
            def tick(value):
                return SimpleNamespace(source_time=stamp, last=value, price=value,
                                       bid=value, ask=value, volume=volume)
            domestic = {s: tick(price + i) for i, s in enumerate(symbols)}
            buffer.observe(at=stamp, au_ticks=domestic,
                xau=tick(4300), fx=tick(7), bands={s: band for s in symbols})
        # International recovery alone is an observation, never a domestic fill.
        stamp = at + timedelta(seconds=3)
        buffer.observe(at=stamp, au_ticks=domestic, xau=tick(4301), fx=tick(7),
                       bands={s: band for s in symbols})
        body = buffer.snapshot(at=at + timedelta(seconds=4))
        body['synthetic'] = True
        def emit(kind, position='complete', payload=None, order=None, trade=None):
            return ledger.emit(kind, trading_day=at.date().isoformat(), occurred_at=at.isoformat(),
                payload=payload or {}, position_id=position, order_id=order, trade_id=trade)
        alert = emit('TARGET_FILL_CONFIRMED', payload={'symbol': symbols[0]})
        for i, (leg, offset, side, price) in enumerate((
            ('target', 'OPEN', 'BUY', 960), ('hedge', 'OPEN', 'SELL', 959),
            ('target', 'CLOSE', 'SELL', 962), ('hedge', 'CLOSE', 'BUY', 960),
        )):
            emit('ACCOUNT_TRADE', payload=dict(account_id='synthetic-account', leg=leg,
                offset=offset, direction=side, price=price, volume=1, multiplier=1000,
                symbol=symbols[0 if leg == 'target' else 1]), order=f'o{i}', trade=f't{i}')
        emit('MODEL_RESULT', payload=dict(net_profit_cny=950, fixed_cost_cny=50))
        emit('TARGET_FILL_CONFIRMED', position='unresolved', payload={'symbol': symbols[2]})
        emit('ACCOUNT_TRADE', position='unresolved', payload=dict(account_id='synthetic-account',
            leg='target', offset='OPEN', direction='BUY', price=960, volume=1, multiplier=1000,
            symbol=symbols[2]), order='o-pending', trade='t-pending')
        sent = []
        def send(payload):
            sent.append(payload)
            return push_snapshot(payload, base + '/api/v1/internal/market/snapshot',
                                 'synthetic-e2e-ingest')
        publisher = AsyncReviewPublisher(tmp_path / 'outbox', send, event_source=ledger.review_events)
        assert publisher.submit_snapshot(body)
        assert publisher.drain_once()
        assert publisher.last_error is None
        assert publisher.health_status()['pending_events'] == 0
        assert publisher.health_status()['pending_snapshots'] == 0
        assert len(mod.latest()['quotes']) == 8
        latest_quote = next(q for q in mod.latest()['quotes'] if q['contract'] == symbols[0])
        assert latest_quote['last_price'] == 962
        assert latest_quote['international_price'] == pytest.approx(4301 * 7 / 31.1034768)
        assert latest_quote['volume_delta'] == 0 and latest_quote['raw_ticks'] == []
        assert datetime.fromisoformat(latest_quote['source_time'].replace('Z', '+00:00')) == at + timedelta(seconds=2)
        assert datetime.fromisoformat(latest_quote['observed_at'].replace('Z', '+00:00')) == at + timedelta(seconds=3)
        # Publisher assigns its durable transport sequence; replay that exact
        # accepted packet, not the pre-outbox source document.
        accepted_snapshot = next(packet for packet in reversed(sent) if not packet.get('event_only'))
        assert push_snapshot(accepted_snapshot, base + '/api/v1/internal/market/snapshot',
                             'synthetic-e2e-ingest')['ok']
        with connect() as conn:
            assert conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0] == 64
        complete = mod.position_view('complete', ledger.run_id)
        assert complete['account']['fixed_cost_net_cny'] == 950
        assert mod.position_view('unresolved', ledger.run_id)['account']['fixed_cost_net_cny'] is None
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=os.getenv(
                'MARKET_TEST_CHROMIUM', '/home/ishelwsl/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome'),
                args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1366, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script('localStorage.setItem("aliecs_auth_token", ' + json.dumps(token) + ')')
            page.goto(base + '/market/')
            page.wait_for_function("window.MarketReview && MarketReview.state.events.length === 8")
            expect(page.locator('#market-overview .market-card')).to_have_count(8)
            assert '合成演示' in page.locator('#demo-banner').inner_text()
            page.locator('[data-event-id="' + alert['event_id'] + '"]').first.click()
            page.wait_for_function("document.querySelector('#position-detail').textContent.includes('950')")
            page.locator('#reason').fill('合成端到端证据；不作真实错单结论')
            page.locator('#annotation-form button').click()
            page.wait_for_function("document.querySelector('#annotations').textContent.includes('合成端到端证据')")
            rows = mod.annotations('complete', ledger.run_id)['annotations']
            assert len(rows) == 1 and rows[0]['author_id'] == 7
            # Invoke the real authenticated endpoint from the browser; no route interception.
            result = page.evaluate('''async ({run, event}) => {
                const headers = {Authorization: 'Bearer ' + localStorage.getItem('aliecs_auth_token')};
                const url = '/api/v1/market/alerts/' + encodeURIComponent(event) + '/read?run_id=' + encodeURIComponent(run);
                return (await fetch(url, {method:'POST', headers})).status;
            }''', dict(run=ledger.run_id, event=alert['event_id']))
            assert result == 200
            assert mod.alert_state(ledger.run_id, 7)['read_event_ids'] == [alert['event_id']]
            assert mod.alert_state(ledger.run_id, 8)['read_event_ids'] == []
            assert mod.position_view('complete', ledger.run_id) == complete
            page.screenshot(path='/tmp/v6-completion-e2e.png', full_page=True)
            page.locator('#logout').click()
            assert page.evaluate("localStorage.getItem('aliecs_auth_token')") is None
            assert page.evaluate('MarketReview.state.quotes.length') == 0
            assert client.get('/api/v1/market/latest').status_code == 401
            # The test's init script deliberately restores its synthetic token
            # on navigation, so revocation can be checked independently of logout.
            page.goto(base + '/market/')
            page.wait_for_function("window.MarketReview && MarketReview.state.events.length === 8")
            with connect() as conn:
                conn.execute('UPDATE users SET token_version=2 WHERE id=7')
            page.evaluate('MarketReview.refresh()')
            page.wait_for_function("document.querySelector('#review-status').textContent.includes('登录')")
            assert not errors
            browser.close()
        assert not [r for r in requests if r[2] >= 500]
        assert any(path.startswith('/api/v1/auth/me') and status == 200 for _, path, status in requests)
        with connect() as conn:
            assert conn.execute('SELECT count(*) FROM market_review_events').fetchone()[0] == 8
    finally:
        if publisher is not None:
            publisher.close(timeout=2)
        ledger.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        client.close()
