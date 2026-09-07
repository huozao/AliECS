"""Real PostgreSQL pagination and incremental snapshot integration."""
import importlib
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def service(monkeypatch):
    url = os.getenv('V6_TEST_DATABASE_URL')
    if not url:
        pytest.skip('V6_TEST_DATABASE_URL not configured')
    import psycopg
    backend = ROOT / 'services/backend-api'
    old = {k: v for k, v in sys.modules.items() if k == 'app' or k.startswith('app.')}
    for k in old:
        del sys.modules[k]
    monkeypatch.syspath_prepend(str(backend))
    mod = importlib.import_module('app.market_review')
    schema = 'review_test_' + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA {schema}')
    connect = lambda: psycopg.connect(url, options=f'-c search_path={schema}')
    with connect() as conn:
        sql = (ROOT / 'db/migrations/0055_market_review.sql').read_text().split('INSERT INTO permissions')[0]
        conn.execute(sql)
    monkeypatch.setattr(mod, '_conn', connect)
    yield mod, connect
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA {schema} CASCADE')
    for k in list(sys.modules):
        if k == 'app' or k.startswith('app.'):
            del sys.modules[k]
    sys.modules.update(old)


def snapshot(sequence, contract='A', run='r', at='2026-09-07T01:00:00Z'):
    return dict(schema_version='market-review.v1', model_version='V6.0',
                run_id=run, sequence=sequence, published_at=at,
                quotes=[dict(contract=contract, source_time=at, last_price=960+sequence)],
                bands=[dict(contract=contract, source_time=at, center=961)], events=[])


def test_latest_keeps_contracts_from_distinct_incremental_packets(service):
    mod, _ = service
    mod.ingest(snapshot(1, 'A'))
    mod.ingest(snapshot(2, 'B', at='2026-09-07T01:00:01Z'))
    latest = mod.latest()
    assert {q['contract'] for q in latest['quotes']} == {'A', 'B'}
    assert {b['contract'] for b in latest['bands']} == {'A', 'B'}


def test_series_cursor_retains_equal_time_rows_and_isolates_runs(service, monkeypatch):
    mod, _ = service
    monkeypatch.setattr(mod, 'SERIES_PAGE_SIZE', 2, raising=False)
    for n in range(1, 5):
        mod.ingest(snapshot(n))
    mod.ingest(snapshot(1, run='other'))
    args = dict(symbol='A', start='2026-09-07T00:00:00Z', end='2026-09-07T02:00:00Z', bucket_ms=1000, run_id='r')
    first = mod.series(**args)
    assert first['has_more']
    second = mod.series(**args, after=first['next_after'])
    assert not second['has_more']
    assert second['next_after'] != first['next_after']
    assert max(q['last_price'] for q in second['quotes']) == 964
    assert all(q['run_id'] == 'r' for q in first['quotes'] + second['quotes'])
    empty = mod.series(**args, after=second['next_after'])
    assert empty['quotes'] == []
    assert empty['next_after'] == second['next_after']
