from __future__ import annotations
import copy
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'services/backend-api'


class MarketReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in list(sys.modules):
            if name == 'app' or name.startswith('app.'):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND))
        cls.service = importlib.import_module('app.market_review')
        cls.router = importlib.import_module('app.routers.market_snapshot')

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name == 'app' or name.startswith('app.'):
                del sys.modules[name]
        sys.path.remove(str(BACKEND))

    def events(self):
        events = []
        for i, (leg, offset, direction, price) in enumerate([
            ('target', 'OPEN', 'BUY', 960), ('hedge', 'OPEN', 'SELL', 959),
            ('target', 'CLOSE', 'SELL', 962), ('hedge', 'CLOSE', 'BUY', 960),
        ], 1):
            events.append({'event_id': f'e{i}', 'run_id': 'run', 'sequence': i,
                'position_id': 'p1', 'event_type': 'ACCOUNT_TRADE', 'trade_id': f't{i}',
                'trading_day': '2026-09-07', 'order_id': f'o{i}',
                'payload': {'account_id': 'synthetic', 'leg': leg, 'offset': offset,
                'direction': direction, 'price': price, 'volume': 1, 'multiplier': 1000}})
        events.append({'event_id': 'model', 'sequence': 5, 'position_id': 'p1', 'event_type': 'MODEL_RESULT',
                       'payload': {'net_profit_cny': 950, 'fixed_cost_cny': 50}})
        return events

    def test_four_account_fills_compute_950_and_missing_close_stays_unknown(self):
        result = self.service.project_position(self.events())
        self.assertEqual(result['account']['gross_profit_cny'], 1000)
        self.assertEqual(result['account']['fixed_cost_net_cny'], 950)
        self.assertIsNone(result['account']['actual_net_cny'])
        self.assertEqual(result['account']['legs']['target']['gross_profit_cny'], 2000)
        missing = self.service.project_position([e for e in self.events() if e['event_id'] != 'e4'])
        self.assertIsNone(missing['account']['fixed_cost_net_cny'])
        self.assertEqual(missing['account']['legs']['hedge']['remaining_volume'], 1)
        self.assertTrue(missing['unresolved'])

    def test_duplicate_account_trade_does_not_double_and_candidates_keep_order(self):
        events = self.events()
        duplicate = copy.deepcopy(events[0]); duplicate['event_id'] = 'repeat'
        events.append(duplicate)
        candidates = [{'rank': 2, 'contract': 'B'}, {'rank': 1, 'contract': 'A'}]
        events.append({'event_type': 'HEDGE_CANDIDATES', 'sequence': 6, 'payload': {'candidates': candidates}})
        result = self.service.project_position(events)
        self.assertEqual(result['account']['legs']['target']['opened_volume'], 1)
        self.assertEqual(result['decisions'][0]['payload']['candidates'], candidates)

    def test_window_boundaries_and_gaps(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self.service.window('2026-09-01T00:00:00Z', '2026-09-07T00:00:00Z', 1000)
        self.assertEqual(self.service.sequence_gaps([1, 3, 5]), [[2, 2], [4, 4]])

    def test_routes_enforce_login_permission_and_window(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        app = FastAPI(); app.include_router(self.router.router)
        client = TestClient(app)
        def unauthorized():
            raise HTTPException(401, 'login required')
        app.dependency_overrides[self.router.require_login] = unauthorized
        self.assertEqual(client.get('/v1/market/latest').status_code, 401)
        app.dependency_overrides[self.router.require_login] = lambda: {'id': 4, 'permissions': []}
        self.assertEqual(client.get('/v1/market/latest').status_code, 403)
        app.dependency_overrides[self.router.require_login] = lambda: {'id': 4, 'permissions': ['market.read']}
        self.assertEqual(client.get('/v1/market/series?symbol=X&start=bad').status_code, 422)
        self.assertEqual(client.get('/v1/market/events?run_id=r&limit=2001').status_code, 422)
