"""Real PostgreSQL pagination and incremental snapshot integration."""
import importlib
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _psql_settings():
    url = os.getenv('V6_TEST_DATABASE_URL')
    container = os.getenv('V6_TEST_POSTGRES_CONTAINER')
    if not url or not container:
        pytest.skip('V6_TEST_DATABASE_URL and V6_TEST_POSTGRES_CONTAINER are required for psql migration tests')
    parsed = urlparse(url)
    if not parsed.username or not parsed.path.strip('/'):
        pytest.skip('V6_TEST_DATABASE_URL must include local PostgreSQL user and database')
    return url, container, parsed.username, parsed.path.strip('/')


def _psql_command(container, user, database, *, pgoptions):
    exec_args = ['docker', 'exec', '-i']
    return exec_args + ['-e', f'PGOPTIONS={pgoptions}', container,
            'psql', '-X', '-U', user, '-d', database, '-v', 'ON_ERROR_STOP=1']


def _migration_sql(name, schema):
    sql = (ROOT / 'db/migrations' / name).read_text()
    return f'SET search_path TO {schema};\n' + sql


@pytest.fixture
def psql_schema():
    url, container, user, database = _psql_settings()
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        pytest.skip('psycopg is required for psql migration tests')
    schema = 'review_psql_' + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA {schema}')
    connect = lambda: psycopg.connect(url, options=f'-c search_path={schema}')
    try:
        with connect() as conn:
            sql = (ROOT / 'db/migrations/0055_market_review.sql').read_text().split('INSERT INTO permissions')[0]
            conn.execute(sql)
        yield schema, connect, container, user, database
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA {schema} CASCADE')


def _run_psql(container, user, database, sql, *, pgoptions='-c statement_timeout=1000 -c lock_timeout=1000'):
    return subprocess.run(
        _psql_command(container, user, database, pgoptions=pgoptions),
        input=sql,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def test_0059_psql_bad_legacy_timestamp_rolls_back_then_retries(psql_schema):
    from psycopg.types.json import Jsonb
    schema, connect, container, user, database = psql_schema
    with connect() as conn:
        conn.execute(
            'INSERT INTO market_review_snapshots(run_id,sequence,published_at,body) VALUES(%s,%s,clock_timestamp(),%s)',
            ('bad', 1, Jsonb({'quotes': [{'contract': 'A', 'source_time': 'not-a-time'}], 'bands': []})),
        )
    failed = _run_psql(container, user, database, _migration_sql('0059_market_review_observations.sql', schema))
    assert failed.returncode != 0
    assert 'invalid input syntax' in failed.stderr
    with connect() as conn:
        assert conn.execute("SELECT to_regclass('market_review_observations') IS NULL").fetchone()[0]
        assert conn.execute("SELECT to_regclass('market_review_current_observations') IS NULL").fetchone()[0]
        assert conn.execute("SELECT NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='market_review_snapshots'::regclass AND tgname='market_review_snapshot_projection')").fetchone()[0]
        conn.execute(
            "UPDATE market_review_snapshots SET body=jsonb_set(body, '{quotes,0,source_time}', '\"2026-09-07T01:00:00Z\"') WHERE run_id='bad'"
        )
    passed = _run_psql(container, user, database, _migration_sql('0059_market_review_observations.sql', schema))
    assert passed.returncode == 0, passed.stderr
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM market_review_snapshots').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0] == 1
        assert conn.execute("SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='market_review_snapshots'::regclass AND tgname='market_review_snapshot_projection')").fetchone()[0]


def test_0059_large_backfill_finishes_without_rewriting_current_for_every_row(psql_schema):
    schema, connect, container, user, database = psql_schema
    with connect() as conn:
        conn.execute("""INSERT INTO market_review_snapshots(run_id,sequence,published_at,body)
            SELECT 'bulk',n,stamp,jsonb_build_object('quotes',jsonb_build_array(
                jsonb_build_object('contract','A','source_time',stamp,'last_price',n)), 'bands','[]'::jsonb)
            FROM (SELECT n,'2026-09-07T00:00:00Z'::timestamptz + n * interval '1 second' AS stamp
                  FROM generate_series(1,50000) n) samples""")
    result = _run_psql(container, user, database,
        _migration_sql('0059_market_review_observations.sql', schema),
        pgoptions='-c statement_timeout=5000 -c lock_timeout=1000')
    assert result.returncode == 0, result.stderr
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM market_review_snapshots').fetchone()[0] == 50000
        assert conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0] == 50000
        assert conn.execute('SELECT sequence,body FROM market_review_current_observations').fetchone() == (
            50000, {'contract': 'A', 'source_time': '2026-09-07T13:53:20+00:00', 'last_price': 50000})


def test_0059_psql_lock_timeout_rolls_back_without_derived_objects(psql_schema):
    schema, connect, container, user, database = psql_schema
    with connect() as blocker:
        blocker.execute('LOCK TABLE market_review_snapshots IN ACCESS EXCLUSIVE MODE')
        failed = _run_psql(container, user, database, _migration_sql('0059_market_review_observations.sql', schema),
                           pgoptions='-c lock_timeout=100 -c statement_timeout=1000')
        assert failed.returncode != 0
        assert 'lock timeout' in failed.stderr
        with connect() as conn:
            assert conn.execute("SELECT to_regclass('market_review_observations') IS NULL").fetchone()[0]
            assert conn.execute("SELECT to_regclass('market_review_current_observations') IS NULL").fetchone()[0]
            assert conn.execute("SELECT NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='market_review_snapshots'::regclass AND tgname='market_review_snapshot_projection')").fetchone()[0]


def test_0060_psql_locks_old_writer_until_backfill_commits(psql_schema):
    from psycopg.types.json import Jsonb
    schema, connect, container, user, database = psql_schema
    with connect() as conn:
        for sequence in (1, 3):
            conn.execute(
                'INSERT INTO market_review_events(event_id,run_id,sequence,occurred_at,body) VALUES(%s,%s,%s,clock_timestamp(),%s)',
                (f'old-{sequence}', 'legacy-window', sequence, Jsonb({'sequence': sequence})),
            )
    sql = _migration_sql('0060_market_review_event_gaps.sql', schema)
    marker = '-- Only runs with no projection are backfilled.'
    prefix, suffix = sql.split(marker, 1)
    application = schema + '_migration'
    process = subprocess.Popen(
        _psql_command(container, user, database,
                      pgoptions=f'-c application_name={application} -c lock_timeout=1000 '
                                '-c statement_timeout=5000 -c idle_in_transaction_session_timeout=10000'),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    output = queue.Queue()
    errors = queue.Queue()
    writer_pid = queue.Queue()
    writer = None

    def collect(stream, label):
        for line in iter(stream.readline, ''):
            output.put((label, line))

    def old_writer():
        try:
            with connect() as conn:
                conn.execute('SET LOCAL statement_timeout=10000')
                writer_pid.put(conn.info.backend_pid)
                conn.execute(
                    'INSERT INTO market_review_events(event_id,run_id,sequence,occurred_at,body) VALUES(%s,%s,%s,clock_timestamp(),%s)',
                    ('old-4', 'legacy-window', 4, Jsonb({'sequence': 4})),
                )
        except Exception as exc:
            errors.put(exc)

    readers = [threading.Thread(target=collect, args=(stream, label))
               for stream, label in ((process.stdout, 'out'), (process.stderr, 'err'))]
    for reader in readers:
        reader.start()
    pid = None
    try:
        process.stdin.write(prefix + "\\echo MIGRATION_READY\n")
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                label, line = output.get(timeout=0.1)
            except queue.Empty:
                continue
            if label == 'out' and line.strip() == 'MIGRATION_READY':
                break
        else:
            pytest.fail('migration did not reach the trigger-installed pause marker')
        writer = threading.Thread(target=old_writer)
        writer.start()
        pid = writer_pid.get(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connect() as observer:
                waiting = observer.execute(
                    "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=%s", (pid,)
                ).fetchone()
            if waiting and waiting[0]:
                break
            time.sleep(0.01)
        else:
            pytest.fail('old writer did not block on the migration table lock')
        process.stdin.write(marker + suffix)
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        writer.join(timeout=5)
        assert not writer.is_alive()
        assert errors.empty(), list(errors.queue)
        with connect() as conn:
            assert conn.execute(
                'SELECT sequence FROM market_review_events ORDER BY sequence'
            ).fetchall() == [(1,), (3,), (4,)]
            assert conn.execute(
                'SELECT max_sequence FROM market_review_event_watermarks WHERE run_id=%s', ('legacy-window',)
            ).fetchone()[0] == 4
            assert conn.execute(
                'SELECT start_sequence,end_sequence FROM market_review_event_gaps WHERE run_id=%s', ('legacy-window',)
            ).fetchall() == [(2, 2)]
    finally:
        if not process.stdin.closed:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        # Terminate the actual database session, not only the docker exec client.
        with connect() as cleanup:
            cleanup.execute(
                'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                'WHERE application_name=%s OR pid=%s', (application, pid if writer is not None and writer.is_alive() else None),
            )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if writer is not None:
            writer.join(timeout=12)
            assert not writer.is_alive()
        for reader in readers:
            reader.join(timeout=5)
            assert not reader.is_alive()
        process.stdout.close()
        process.stderr.close()


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
        conn.execute((ROOT / 'db/migrations/0058_market_review_alert_reads.sql').read_text())
        conn.execute((ROOT / 'db/migrations/0059_market_review_observations.sql').read_text())
        conn.execute((ROOT / 'db/migrations/0060_market_review_event_gaps.sql').read_text())
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


def test_isolated_snapshot_projection_cost_evidence(service):
    """Report comparable trigger/JSON write costs without touching production."""
    import json
    mod, connect = service
    count, packets = 40, 20

    def body(sequence, run):
        value = snapshot(sequence, run=run, at=f'2026-09-07T00:00:{sequence:02d}Z')
        value['quotes'] = [dict(contract=f'Q{ordinal}', source_time=value['published_at'],
                                observed_at=value['published_at'], last_price=960 + ordinal)
                           for ordinal in range(count)]
        value['bands'] = [dict(contract=f'B{ordinal}', source_time=value['published_at'],
                               observed_at=value['published_at'], center=960 + ordinal)
                          for ordinal in range(count)]
        return value

    projected = [body(sequence, 'projection') for sequence in range(1, packets + 1)]
    wire_bytes = len(json.dumps(projected[0], separators=(',', ':')).encode())
    elapsed = []
    for value in projected:
        started = time.monotonic()
        assert mod.ingest(value)['ok']
        elapsed.append((time.monotonic() - started) * 1000)
    with connect() as conn:
        observed = conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0]
        current = conn.execute('SELECT count(*) FROM market_review_current_observations').fetchone()[0]
        plan = conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
            "INSERT INTO market_review_snapshots(run_id,sequence,published_at,body) "
            "VALUES('plan',1,clock_timestamp(),'{\"quotes\":[],\"bands\":[]}'::jsonb)"
        ).fetchone()[0][0]
        conn.execute('DROP TRIGGER market_review_snapshot_projection ON market_review_snapshots')
    base_elapsed = []
    for value in (body(sequence, 'without-projection') for sequence in range(1, packets + 1)):
        started = time.monotonic()
        assert mod.ingest(value)['ok']
        base_elapsed.append((time.monotonic() - started) * 1000)
    assert observed == packets * count * 2
    assert current == count * 2
    assert plan['Execution Time'] >= 0
    ordered = sorted(elapsed)
    base_ordered = sorted(base_elapsed)
    print('isolated_snapshot_projection: packets=%d json_bytes=%d observations=%d current=%d '
          'with_trigger_ms_p50=%.3f with_trigger_ms_p95=%.3f with_trigger_ms_max=%.3f '
          'without_trigger_ms_p50=%.3f without_trigger_ms_p95=%.3f plan_ms=%.3f' % (
              packets, wire_bytes, observed, current, ordered[len(ordered) // 2],
              ordered[min(len(ordered) - 1, int(len(ordered) * .95))], max(ordered),
              base_ordered[len(base_ordered) // 2],
              base_ordered[min(len(base_ordered) - 1, int(len(base_ordered) * .95))],
              plan['Execution Time']))


def test_latest_keeps_contracts_from_distinct_incremental_packets(service):
    mod, _ = service
    mod.ingest(snapshot(1, 'A'))
    mod.ingest(snapshot(2, 'B', at='2026-09-07T01:00:01Z'))
    latest = mod.latest()
    assert {q['contract'] for q in latest['quotes']} == {'A', 'B'}
    assert {b['contract'] for b in latest['bands']} == {'A', 'B'}


def test_latest_accepts_equal_source_time_refresh_from_independent_model_source(service):
    mod, _ = service
    first = snapshot(1, at='2026-09-07T01:00:00Z')
    second = snapshot(2, at='2026-09-07T01:00:01Z')
    second['quotes'][0]['source_time'] = first['quotes'][0]['source_time']
    second['quotes'][0]['international_price'] = 961.25
    second['bands'][0]['source_time'] = first['bands'][0]['source_time']
    second['bands'][0]['center'] = 963
    mod.ingest(first)
    mod.ingest(second)
    latest = mod.latest()
    assert latest['quotes'][0]['international_price'] == 961.25
    assert latest['bands'][0]['center'] == 963


def test_alert_read_state_is_per_user_and_idempotent(service):
    mod, connect = service
    event = {'event_id': 'target-fill-1', 'run_id': 'r', 'sequence': 1,
             'position_id': 'p', 'event_type': 'TARGET_FILL_CONFIRMED｜目标腿成交确认',
             'trading_day': '2026-09-07', 'model_version': 'V6.0',
             'world_id': None, 'order_id': None, 'trade_id': None,
             'occurred_at': '2026-09-07T01:00:00Z', 'recorded_at': '2026-09-07T01:00:00Z',
             'payload': {'symbol': 'SHFE.au2612'}}
    body = snapshot(1); body['events'] = [event]
    mod.ingest(body)
    assert mod.alert_state('r', 1)['alerts'][0]['event_id'] == 'target-fill-1'
    first_read = mod.mark_alert_read('r', 'target-fill-1', 1)
    assert mod.mark_alert_read('r', 'target-fill-1', 1) == first_read
    assert mod.alert_state('r', 1)['read_event_ids'] == ['target-fill-1']
    assert mod.alert_state('r', 2)['read_event_ids'] == []
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM market_review_event_reads').fetchone()[0] == 1
        assert conn.execute('SELECT body FROM market_review_events').fetchone()[0] == event


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
    assert second['has_more']
    assert second['next_after'] != first['next_after']
    third = mod.series(**args, after=second['next_after'])
    fourth = mod.series(**args, after=third['next_after'])
    assert not fourth['has_more']
    assert [q['last_price'] for p in (first, second, third, fourth) for q in p['quotes']] == [961, 962, 963, 964]
    assert all(q['run_id'] == 'r' for q in first['quotes'] + second['quotes'])
    empty = mod.series(**args, after=fourth['next_after'])
    assert empty['quotes'] == []
    assert empty['next_after'] == fourth['next_after']


def test_observation_window_retains_independent_refresh_and_late_publication(service, monkeypatch):
    mod, _ = service
    monkeypatch.setattr(mod, 'SERIES_PAGE_SIZE', 2)
    for n in range(1, 4):
        body = snapshot(n, at='2026-09-07T03:00:00Z')
        for field in ('quotes', 'bands'):
            body[field][0].update(source_time='2026-09-07T00:59:00Z',
                                  observed_at=f'2026-09-07T01:00:0{n}Z')
        body['quotes'][0].update(volume_delta=0, volume_delta_reason='UNCHANGED',
                                  hedge_previews=[{'target_side': 'buy', 'candidates': []}])
        mod.ingest(body)
    mod.ingest(snapshot(1, 'B', run='other', at='2026-09-07T01:00:01Z'))
    args = dict(symbol='*', start='2026-09-07T01:00:00Z', end='2026-09-07T01:00:04Z', bucket_ms=1000, run_id='r')
    quotes, bands, cursor = [], [], None
    for _ in range(8):
        page = mod.series(**args, after=cursor)
        quotes.extend(page['quotes']); bands.extend(page['bands'])
        cursor = page['next_after']
        if not page['has_more']:
            break
    assert [q['last_price'] for q in quotes] == [961, 962, 963]
    assert len(bands) == 3
    assert {q['run_id'] for q in quotes} == {'r'}
    assert all(q['volume_delta'] == 0 and q['volume_delta_reason'] == 'UNCHANGED' for q in quotes)
    assert quotes[0]['hedge_previews'] == [{'target_side': 'buy', 'candidates': []}]
    assert len({q['observed_at'] for q in quotes}) == 3


def test_all_contracts_legacy_capture_fallback_and_subsecond_versions(service):
    mod, _ = service
    for n, contract in enumerate(('A', 'B', 'A'), 1):
        body = snapshot(n, contract, at='2026-09-07T02:00:00Z')
        for field in ('quotes', 'bands'):
            body[field][0].update(source_time='2026-09-07T00:00:00Z',
                                  captured_at=f'2026-09-07T01:00:00.{n}00Z')
        mod.ingest(body)
    mod.ingest(snapshot(4, 'C', at='2026-09-07T01:00:00Z'))
    page = mod.series('*', '2026-09-07T01:00:00Z', '2026-09-07T01:00:01Z', 1000, run_id='r')
    assert len(page['quotes']) == 4
    assert {q['contract'] for q in page['quotes']} == {'A', 'B', 'C'}
    assert all(q.get('observed_at') for q in page['quotes'])


def test_latest_retains_low_frequency_contract_and_rejects_source_and_observation_regression(service):
    mod, _ = service
    mod.ingest(snapshot(1, 'B'))
    for n in range(2, 204):
        body = snapshot(n, 'A', at='2026-09-07T01:02:00Z')
        for field in ('quotes', 'bands'):
            body[field][0]['observed_at'] = '2026-09-07T01:03:00Z'
        mod.ingest(body)
    older_observation = snapshot(204, 'A', at='2026-09-07T01:04:00Z')
    older_source = snapshot(205, 'A', at='2026-09-07T01:05:00Z')
    for field in ('quotes', 'bands'):
        older_observation[field][0].update(source_time='2026-09-07T01:02:00Z', observed_at='2026-09-07T01:02:30Z')
        older_source[field][0].update(source_time='2026-09-07T01:01:00Z', observed_at='2026-09-07T01:05:00Z')
    mod.ingest(older_observation); mod.ingest(older_source)
    latest = mod.latest()
    assert {q['contract'] for q in latest['quotes']} == {'A', 'B'}
    assert next(q for q in latest['quotes'] if q['contract'] == 'A')['last_price'] == 1163


def event(sequence, kind, position='p', run='r', payload=None):
    return {'event_id': f'{run}-{sequence}', 'run_id': run, 'sequence': sequence,
            'position_id': position, 'event_type': kind, 'trading_day': '2026-09-07',
            'model_version': 'V6.0', 'world_id': None, 'order_id': f'o{sequence}',
            'trade_id': f't{sequence}', 'occurred_at': '2026-09-07T01:00:00Z',
            'recorded_at': '2026-09-07T01:00:00Z', 'payload': payload or {}}


def test_alerts_exact_target_fills_pagination_multiple_positions_and_immutable_reads(service):
    mod, connect = service
    trade = dict(account_id='synthetic', leg='target', offset='OPEN', direction='BUY', price=960, volume=1, multiplier=1000)
    records = [event(1, 'TARGET_FILL_REJECTED'), event(2, 'ORDER_SENT'),
               event(3, 'TARGET_FILL_CONFIRMED｜目标腿成交确认'),
               event(4, 'TARGET_FILL_CONFIRMED', position='p2'),
               event(5, 'ACCOUNT_TRADE', payload=trade),
               event(6, 'ACCOUNT_TRADE', payload=dict(trade, leg='hedge')),
               event(7, 'ACCOUNT_TRADE', payload=dict(trade, offset='CLOSE', direction='SELL'))]
    body = snapshot(1); body['events'] = records
    mod.ingest(body); mod.ingest(body)
    first = mod.alert_state('r', 1, limit=2)
    assert [e['sequence'] for e in first['alerts']] == [3, 4]
    assert first['next_sequence'] == 4 and not first['has_more']
    limited = mod.alert_state('r', 1, limit=1)
    assert limited['next_sequence'] == 3 and limited['has_more']
    assert mod.alert_state('r', 1, after_sequence=limited['next_sequence'], limit=1)['alerts'][0]['event_id'] == 'r-4'
    second = mod.alert_state('r', 1, after_sequence=4, limit=2)
    assert second['alerts'] == []
    assert second['next_sequence'] == 4 and not second['has_more']
    before = mod.position_view('p', 'r')
    read = mod.mark_alert_read('r', 'r-3', 1)
    assert mod.mark_alert_read('r', 'r-3', 1) == read
    assert mod.alert_state('r', 2)['read_event_ids'] == []
    assert mod.alert_state('other', 1)['alerts'] == []
    assert mod.position_view('p', 'r') == before
    with pytest.raises(mod.HTTPException) as exc:
        mod.mark_alert_read('r', 'r-1', 1)
    assert exc.value.status_code == 404
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM market_review_events').fetchone()[0] == 7


def test_authenticated_api_uses_real_uid_and_annotations_are_append_only(service, monkeypatch):
    import time
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    mod, connect = service
    core = importlib.import_module('app.core')
    router = importlib.import_module('app.routers.market_snapshot')
    monkeypatch.setenv('AUTH_TOKEN_SECRET', 'synthetic-market-test-secret')
    monkeypatch.setattr(core, '_conn', connect)
    with connect() as conn:
        conn.execute('CREATE TABLE users (id bigint PRIMARY KEY, token_version integer)')
        conn.execute('INSERT INTO users VALUES (41, 1), (42, 1)')
    app = FastAPI(); app.include_router(router.router)
    client = TestClient(app, raise_server_exceptions=False)
    def headers(uid=41, permissions=None, **extra):
        payload = dict(uid=uid, sub='synthetic-reviewer', tv=1, exp=int(time.time()) + 600,
                       permissions=permissions if permissions is not None else ['market.read', 'market.annotate'])
        payload.update(extra)
        return {'Authorization': 'Bearer ' + core._encode_token(payload)}
    body = snapshot(1); body['events'] = [event(1, 'TARGET_FILL_CONFIRMED')]
    mod.ingest(body)
    assert client.get('/v1/market/alerts?run_id=r', headers=headers()).status_code == 200
    response = client.post('/v1/market/alerts/r-1/read?run_id=r', headers=headers())
    assert response.status_code == 200, response.text
    assert client.get('/v1/market/alerts?run_id=r', headers=headers(42)).json()['read_event_ids'] == []
    annotation = dict(run_id='r', position_id='p', verdict='uncertain', reason='synthetic evidence', evidence_ids=['r-1'])
    before = mod.position_view('p', 'r')
    denied = client.post('/v1/market/annotations', json=annotation, headers=headers(permissions=['market.read']))
    assert denied.status_code == 403
    for revision in (1, 2):
        response = client.post('/v1/market/annotations', json=annotation, headers=headers())
        assert response.status_code == 200, response.text
        assert response.json()['revision'] == revision
    rows = client.get('/v1/market/annotations?run_id=r&position_id=p', headers=headers()).json()['annotations']
    assert [row['author_id'] for row in rows] == [41, 41]
    assert [row['author_name'] for row in rows] == ['synthetic-reviewer', 'synthetic-reviewer']
    assert mod.position_view('p', 'r') == before
    assert client.post('/v1/market/annotations', json=dict(annotation, author_id=42), headers=headers()).status_code == 422
    reads = ['/v1/market/latest', '/v1/market/series?symbol=*', '/v1/market/events?run_id=r',
             '/v1/market/alerts?run_id=r', '/v1/market/positions/p?run_id=r',
             '/v1/market/annotations?run_id=r&position_id=p']
    for path in reads:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=headers(permissions=[])).status_code == 403
        assert client.get(path, headers=headers(exp=1)).status_code == 401
    with connect() as conn:
        conn.execute('UPDATE users SET token_version=2 WHERE id=41')
    assert client.get('/v1/market/alerts?run_id=r', headers=headers()).status_code == 401


def test_persisted_complete_and_unresolved_positions_keep_frozen_decisions_and_late_fills(service):
    mod, connect = service
    records = []
    for sequence, (leg, offset, direction, price) in enumerate([
        ('target', 'OPEN', 'BUY', 960), ('hedge', 'OPEN', 'SELL', 959),
        ('target', 'CLOSE', 'SELL', 962), ('hedge', 'CLOSE', 'BUY', 960),
    ], 1):
        records.append(event(sequence, 'ACCOUNT_TRADE', payload=dict(
            account_id='synthetic', leg=leg, offset=offset, direction=direction,
            price=price, volume=1, multiplier=1000)))
    records.append(event(5, 'MODEL_RESULT', payload=dict(net_profit_cny=950, fixed_cost_cny=50)))
    candidates = [{'symbol': 'A', 'rank': 2, 'eligible': False, 'reason_code': 'STALE'},
                  {'symbol': 'B', 'rank': 1, 'eligible': True}]
    records.append(event(6, 'HEDGE_CANDIDATES', payload=dict(candidates=candidates, selected_symbol='B')))
    first = snapshot(1); first['events'] = [r for r in records if r['sequence'] != 4]
    mod.ingest(first)
    before = mod.position_view('p', 'r')
    assert before['unresolved']
    assert before['account']['fixed_cost_net_cny'] is None
    assert before['account']['legs']['hedge']['remaining_volume'] == 1
    # New run and repeated old packet must preserve unresolved old-run evidence.
    mod.ingest(snapshot(1, run='next-run', at='2026-09-08T01:00:00Z'))
    mod.ingest(first)
    assert mod.latest()['unresolved_positions'][0]['run_id'] == 'r'
    with pytest.raises(mod.HTTPException) as exc:
        mod.position_view('p', 'next-run')
    assert exc.value.status_code == 404
    late = snapshot(2); late['event_only'] = True; late['events'] = [records[3]]
    mod.ingest(late); mod.ingest(late)
    complete = mod.position_view('p', 'r')
    assert not complete['unresolved']
    assert complete['account']['gross_profit_cny'] == 1000
    assert complete['account']['fixed_cost_net_cny'] == 950
    assert complete['account']['actual_net_cny'] is None
    assert complete['decisions'][0]['payload']['candidates'] == candidates
    assert len(complete['events']) == 6
    assert mod.latest()['unresolved_positions'] == []
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM market_review_events').fetchone()[0] == 6


def test_series_rejects_cross_run_cursor_and_accepts_legacy_publication_cursor(service):
    import base64
    import json
    mod, _ = service
    mod.ingest(snapshot(1))
    mod.ingest(snapshot(2, at='2026-09-07T01:00:01Z'))
    args = dict(symbol='A', start='2026-09-07T01:00:00Z', end='2026-09-07T01:00:02Z', bucket_ms=1000, run_id='r')
    page = mod.series(**args)
    with pytest.raises(mod.HTTPException) as exc:
        mod.series(**dict(args, run_id='other'), after=page['next_after'])
    assert exc.value.status_code == 422
    old = 'v1.' + base64.urlsafe_b64encode(json.dumps(['2026-09-07T01:00:00Z', 'r', 1]).encode()).decode().rstrip('=')
    assert [q['last_price'] for q in mod.series(**args, after=old)['quotes']] == [962]


@pytest.mark.parametrize('field', ['quotes', 'bands'])
@pytest.mark.parametrize('time_field', ['observed_at', 'captured_at', 'source_time'])
def test_ingest_rejects_invalid_evidence_times_before_database_queries(service, field, time_field):
    mod, _ = service
    body = snapshot(1); body[field][0][time_field] = 'not-a-time'
    with pytest.raises(mod.HTTPException) as exc:
        mod.ingest(body)
    assert exc.value.status_code == 422


def test_observation_projection_is_atomic_idempotent_and_keeps_source_time(service):
    mod, connect = service
    body = snapshot(1)
    body['quotes'][0]['observed_at'] = '2026-09-07T01:01:00Z'
    body['display_evidence'] = {'dropped_observations': 7}
    mod.ingest(body); mod.ingest(body)
    with connect() as conn:
        observations = conn.execute('SELECT field,source_time,observed_at FROM market_review_observations ORDER BY field').fetchall()
        assert len(observations) == 2
        assert observations[1][1].isoformat() == '2026-09-07T01:00:00+00:00'
        assert observations[1][2].isoformat() == '2026-09-07T01:01:00+00:00'
        assert conn.execute('SELECT count(*) FROM market_review_current_observations').fetchone()[0] == 2
    assert mod.latest()['display_evidence'] == {'dropped_observations': 7}
    conflicting = snapshot(2); conflicting['events'] = [event(1, 'TARGET_FILL_CONFIRMED')]
    mod.ingest(conflicting)
    rejected = snapshot(3); rejected['events'] = [event(1, 'ORDER_SENT')]
    with pytest.raises(mod.HTTPException) as exc:
        mod.ingest(rejected)
    assert exc.value.status_code == 409
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0] == 4
        assert conn.execute('SELECT count(*) FROM market_review_snapshots').fetchone()[0] == 2


def test_old_backend_insert_during_rollout_is_projected_in_its_transaction(service):
    from psycopg.types.json import Jsonb
    mod, connect = service
    body = snapshot(1); body.pop('events')
    with connect() as conn:
        conn.execute('INSERT INTO market_review_snapshots(run_id,sequence,published_at,body) VALUES(%s,%s,%s,%s)',
                     ('r', 1, body['published_at'], Jsonb(body)))
        assert conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0] == 2
    assert mod.latest()['quotes'][0]['last_price'] == 961


def test_current_observation_rejects_new_source_with_older_observation_including_backfill(service):
    mod, connect = service
    first = snapshot(1)
    second = snapshot(2, at='2026-09-07T01:04:00Z')
    for field in ('quotes', 'bands'):
        first[field][0]['observed_at'] = '2026-09-07T01:03:00Z'
        second[field][0].update(source_time='2026-09-07T01:01:00Z', observed_at='2026-09-07T01:02:00Z')
    mod.ingest(first); mod.ingest(second)
    assert mod.latest()['quotes'][0]['last_price'] == 961
    with connect() as conn:
        # Reconstruct disposable derived state as an older installation would.
        conn.execute('DELETE FROM market_review_current_observations')
        conn.execute((ROOT / 'db/migrations/0059_market_review_observations.sql').read_text())
    assert mod.latest()['quotes'][0]['last_price'] == 961
    rows = mod.series('A', '2026-09-07T01:00:00Z', '2026-09-07T01:04:00Z', 1000, run_id='r')['quotes']
    assert len(rows) == 2


def test_grouped_backfill_matches_incremental_projection_with_null_and_crossed_clocks(service):
    import random
    from datetime import datetime, timedelta, timezone
    from psycopg.types.json import Jsonb
    _, connect = service
    randomizer = random.Random(509)
    origin = datetime(2026, 9, 7, tzinfo=timezone.utc)
    def stamp():
        offset = randomizer.choice([None, 0, 1, 2, 3, 4, 5])
        return None if offset is None else (origin + timedelta(seconds=offset)).isoformat()
    with connect() as conn:
        for run in ('first', 'second'):
            for sequence in range(1, 41):
                body = {field: [{'contract': contract, 'source_time': stamp(),
                    'observed_at': stamp(), 'marker': [run, sequence, field, ordinal]}
                    for ordinal, contract in enumerate(contracts)]
                    for field, contracts in [('quotes', ['A', 'B', 'A']), ('bands', ['A', 'C'])]}
                conn.execute('INSERT INTO market_review_snapshots(run_id,sequence,published_at,body) VALUES(%s,%s,%s,%s)',
                    (run, sequence, origin, Jsonb(body)))
        query = 'SELECT run_id,field,contract,sequence,ordinal,body FROM market_review_current_observations ORDER BY run_id,field,contract'
        # The unchanged online trigger is an independent implementation of the
        # acceptance rule, exercised before the grouped migration runs.
        expected = conn.execute(query).fetchall()
        assert len(expected) == 8
        assert conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0] == 400
        conn.execute('DELETE FROM market_review_current_observations')
        migration = (ROOT / 'db/migrations/0059_market_review_observations.sql').read_text()
        conn.execute(migration)
        assert conn.execute(query).fetchall() == expected
        conn.execute(migration)
        assert conn.execute(query).fetchall() == expected
        assert conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0] == 400


def test_backfill_rerun_preserves_current_from_out_of_order_crossed_clocks(service):
    from psycopg.types.json import Jsonb
    _, connect = service
    with connect() as conn:
        for sequence, source, observed in [(2, 2, 1), (1, 1, 2)]:
            body = {'quotes': [{'contract': 'A',
                'source_time': f'2026-09-07T00:00:0{source}Z',
                'observed_at': f'2026-09-07T00:00:0{observed}Z'}], 'bands': []}
            conn.execute('INSERT INTO market_review_snapshots(run_id,sequence,published_at,body) VALUES(%s,%s,%s,%s)',
                ('out-of-order', sequence, '2026-09-07T00:00:05Z', Jsonb(body)))
        query = 'SELECT sequence FROM market_review_current_observations'
        assert conn.execute(query).fetchall() == [(2,)]
        conn.execute((ROOT / 'db/migrations/0059_market_review_observations.sql').read_text())
        assert conn.execute(query).fetchall() == [(2,)]
        assert conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0] == 2


def test_projection_migration_backfills_legacy_packets_once_without_losing_raw_evidence(service):
    from psycopg.types.json import Jsonb
    mod, connect = service
    body = snapshot(8, 'quiet')
    body.pop('events')
    with connect() as conn:
        conn.execute('DROP TRIGGER market_review_snapshot_projection ON market_review_snapshots')
        conn.execute('INSERT INTO market_review_snapshots(run_id,sequence,published_at,body) VALUES(%s,%s,%s,%s)',
                     ('r', 8, body['published_at'], Jsonb(body)))
        migration = (ROOT / 'db/migrations/0059_market_review_observations.sql').read_text()
        conn.execute(migration); conn.execute(migration)
        assert conn.execute('SELECT body FROM market_review_snapshots').fetchone()[0] == body
        assert conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0] == 2
    page = mod.series('*', '2026-09-07T01:00:00Z', '2026-09-07T01:00:01Z', 1000, run_id='r')
    assert page['quotes'][0]['contract'] == 'quiet'
    assert mod.latest()['quotes'][0]['contract'] == 'quiet'


def test_long_run_window_reads_use_observation_indexes_without_unpacking_snapshots(service, monkeypatch):
    import psycopg
    mod, connect = service
    with connect() as conn:
        conn.execute('DROP TRIGGER market_review_snapshot_projection ON market_review_snapshots')
        conn.execute("""INSERT INTO market_review_snapshots(run_id,sequence,published_at,body)
            SELECT 'r',n,stamp,jsonb_build_object('run_id','r','sequence',n,'published_at',stamp,
                'quotes',jsonb_build_array(jsonb_build_object('contract','A','source_time',stamp,'last_price',960)),
                'bands',jsonb_build_array(jsonb_build_object('contract','A','source_time',stamp,'center',961)))
            FROM (SELECT n,'2026-09-07T00:00:00Z'::timestamptz + n * interval '1 second' AS stamp
                  FROM generate_series(1,10000) n) samples""")
        conn.execute((ROOT / 'db/migrations/0059_market_review_observations.sql').read_text())
        conn.execute('ANALYZE market_review_observations')
        assert conn.execute('SELECT count(*) FROM market_review_observations').fetchone()[0] == 20000
    plans = []
    class MeasuredCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            if query.lstrip().startswith('SELECT'):
                super().execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) ' + query, params, **kwargs)
                plans.append(self.fetchone()[0][0])
            return super().execute(query, params, **kwargs)
    def measured_connect():
        conn = connect(); conn.cursor_factory = MeasuredCursor
        return conn
    monkeypatch.setattr(mod, '_conn', measured_connect)
    for symbol in ('A', '*'):
        result = mod.series(symbol, '2026-09-07T01:00:00Z', '2026-09-07T01:00:01Z', 1000, run_id='r')
        assert len(result['quotes']) == len(result['bands']) == 1
    def nodes(node):
        yield node
        for child in node.get('Plans', []):
            yield from nodes(child)
    for plan in plans:
        steps = list(nodes(plan['Plan']))
        assert any('Index' in step['Node Type'] and 'market_review_observations' in step.get('Index Name', '') for step in steps)
        assert not any(step.get('Relation Name') == 'market_review_snapshots' for step in steps)
        assert max(step.get('Actual Rows', 0) for step in steps) <= 2
    print('observation_index_probe: historical_rows=20000 returned_rows=2 '
          'query_ms=' + ','.join(str(plan['Execution Time']) for plan in plans))


def test_event_gap_projection_splits_shrinks_resolves_and_rolls_back(service):
    mod, connect = service
    def upload(seq, run='r'):
        body = snapshot(seq, run=run); body['event_only'] = True
        body['events'] = [event(seq, 'EVIDENCE', position=None, run=run)]
        return mod.ingest(body)
    assert upload(1)['missing_sequence_ranges'] == []
    assert upload(3)['missing_sequence_ranges'] == [[2, 2]]
    assert upload(2)['missing_sequence_ranges'] == []
    assert upload(10)['missing_sequence_ranges'] == [[4, 9]]
    assert upload(7)['missing_sequence_ranges'] == [[4, 6], [8, 9]]
    assert upload(4)['missing_sequence_ranges'] == [[5, 6], [8, 9]]
    assert upload(9)['missing_sequence_ranges'] == [[5, 6], [8, 8]]
    assert upload(8)['missing_sequence_ranges'] == [[5, 6]]
    assert upload(8)['missing_sequence_ranges'] == [[5, 6]]
    assert upload(1, 'other')['missing_sequence_ranges'] == []
    assert mod.events_page('r', 10, 2)['event_sequence_high_watermark'] == 10
    assert not mod.events_page('r', 10, 2)['missing_sequence_ranges_truncated']
    conflict = snapshot(11); conflict['event_only'] = True
    conflict['events'] = [event(11, 'EVIDENCE', position=None), event(3, 'CONFLICT', position=None)]
    with pytest.raises(mod.HTTPException) as exc:
        mod.ingest(conflict)
    assert exc.value.status_code == 409
    with connect() as conn:
        assert conn.execute('SELECT max_sequence FROM market_review_event_watermarks WHERE run_id=%s', ('r',)).fetchone()[0] == 10
        assert conn.execute('SELECT start_sequence,end_sequence FROM market_review_event_gaps WHERE run_id=%s ORDER BY start_sequence', ('r',)).fetchall() == [(5, 6)]


def test_event_gap_old_writers_concurrently_fill_one_run(service):
    from concurrent.futures import ThreadPoolExecutor
    from psycopg.types.json import Jsonb
    mod, connect = service
    def old_insert(seq):
        record = event(seq, 'EVIDENCE', position=None)
        with connect() as conn:
            conn.execute('SET LOCAL statement_timeout=10000')
            conn.execute('INSERT INTO market_review_events(event_id,run_id,sequence,occurred_at,body) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                         (record['event_id'], 'r', seq, record['occurred_at'], Jsonb(record)))
    old_insert(100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(old_insert, list(range(99, 0, -1)) + [2, 30, 75, 100]))
    page = mod.events_page('r', 100, 2)
    assert page['missing_sequence_ranges'] == []
    assert page['event_sequence_high_watermark'] == 100
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM market_review_events').fetchone()[0] == 100


def test_overlapping_old_writer_batches_lock_run_before_event_unique_indexes(service):
    import queue
    import time
    from concurrent.futures import ThreadPoolExecutor
    from psycopg.types.json import Jsonb
    mod, connect = service
    worker_pid = queue.Queue()
    def insert(conn, sequence):
        record = event(sequence, 'EVIDENCE', position=None)
        conn.execute('INSERT INTO market_review_events(event_id,run_id,sequence,occurred_at,body) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                     (record['event_id'], 'r', sequence, record['occurred_at'], Jsonb(record)))
    def overlapping_writer():
        with connect() as conn:
            conn.execute('SET LOCAL statement_timeout=10000')
            worker_pid.put(conn.info.backend_pid)
            insert(conn, 2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with connect() as first:
            first.execute('SET LOCAL statement_timeout=10000')
            insert(first, 1)
            future = pool.submit(overlapping_writer)
            pid = worker_pid.get(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with connect() as observer:
                    waiting = observer.execute("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone()
                if waiting and waiting[0]:
                    break
                time.sleep(0.01)
            else:
                pytest.fail('concurrent writer did not reach its run lock')
            insert(first, 2)
        future.result(timeout=5)
    assert mod.events_page('r', 2, 2)['missing_sequence_ranges'] == []


def test_event_gap_migration_backfill_and_truncation(service):
    mod, connect = service
    with connect() as conn:
        # Simulate data written before 0060 existed; preserve all original rows.
        trigger = conn.execute("SELECT 1 FROM pg_trigger WHERE tgrelid='market_review_events'::regclass AND tgname='market_review_event_gap_projection'").fetchone()
        if trigger:
            conn.execute('DROP TRIGGER market_review_event_gap_projection ON market_review_events')
        conn.execute("""INSERT INTO market_review_events(event_id,run_id,sequence,occurred_at,body)
            SELECT 'old-'||n,'legacy',n,'2026-09-07T01:00:00Z'::timestamptz,
                   jsonb_build_object('sequence',n) FROM generate_series(2,5002,2) n""")
        migration = (ROOT / 'db/migrations/0060_market_review_event_gaps.sql').read_text()
        conn.execute(migration); conn.execute(migration)
        assert conn.execute('SELECT count(*) FROM market_review_events').fetchone()[0] == 2501
        assert conn.execute('SELECT count(*) FROM market_review_event_gaps').fetchone()[0] == 2501
    page = mod.events_page('legacy', 5002, 2)
    assert len(page['missing_sequence_ranges']) == 2000
    assert page['missing_sequence_ranges'][0] == [1, 1]
    assert page['missing_sequence_ranges'][-1] == [3999, 3999]
    assert page['missing_sequence_ranges_truncated']
    assert page['event_sequence_high_watermark'] == 5002
    body = snapshot(1, run='legacy'); body['event_only'] = True
    assert mod.ingest(body)['missing_sequence_ranges_truncated']


def test_event_gap_migration_rebuilds_legacy_run_before_accepting_new_writer(service):
    from psycopg.types.json import Jsonb
    mod, connect = service
    with connect() as conn:
        conn.execute('DROP TRIGGER market_review_event_gap_projection ON market_review_events')
        conn.execute('DROP TRIGGER market_review_event_run_lock ON market_review_events')
        conn.execute('DELETE FROM market_review_event_gaps')
        conn.execute('DELETE FROM market_review_event_watermarks')
        for sequence in (1, 3):
            record = event(sequence, 'EVIDENCE', position=None, run='legacy-window')
            conn.execute(
                'INSERT INTO market_review_events(event_id,run_id,sequence,occurred_at,body) VALUES(%s,%s,%s,%s,%s)',
                (record['event_id'], record['run_id'], sequence, record['occurred_at'], Jsonb(record)),
            )
        conn.execute((ROOT / 'db/migrations/0060_market_review_event_gaps.sql').read_text())
        assert conn.execute(
            'SELECT start_sequence,end_sequence FROM market_review_event_gaps WHERE run_id=%s',
            ('legacy-window',),
        ).fetchall() == [(2, 2)]
        record = event(4, 'EVIDENCE', position=None, run='legacy-window')
        conn.execute(
            'INSERT INTO market_review_events(event_id,run_id,sequence,occurred_at,body) VALUES(%s,%s,%s,%s,%s)',
            (record['event_id'], record['run_id'], 4, record['occurred_at'], Jsonb(record)),
        )
        assert conn.execute(
            'SELECT start_sequence,end_sequence FROM market_review_event_gaps WHERE run_id=%s',
            ('legacy-window',),
        ).fetchall() == [(2, 2)]


def test_million_event_run_incremental_reads_do_not_scan_event_history(service, monkeypatch):
    import psycopg
    mod, connect = service
    with connect() as conn:
        conn.execute('DROP TRIGGER market_review_event_gap_projection ON market_review_events')
        conn.execute('DROP TRIGGER market_review_event_run_lock ON market_review_events')
        conn.execute("""INSERT INTO market_review_events(event_id,run_id,sequence,occurred_at,body)
            SELECT 'bulk-'||n,'r',n,'2026-09-07T01:00:00Z'::timestamptz,
                   jsonb_build_object('sequence',n) FROM generate_series(1,1000000) n""")
        conn.execute((ROOT / 'db/migrations/0060_market_review_event_gaps.sql').read_text())
        conn.execute('ANALYZE market_review_events')
        conn.execute('ANALYZE market_review_event_gaps')
        conn.execute('ANALYZE market_review_event_watermarks')
        assert conn.execute('SELECT count(*) FROM market_review_events').fetchone()[0] == 1000000
    plans = []
    class MeasuredCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            if query.lstrip().startswith('SELECT') and any(table in query for table in (
                    'market_review_events', 'market_review_event_gaps', 'market_review_event_watermarks')):
                super().execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) ' + query, params, **kwargs)
                plans.append(self.fetchone()[0][0])
            return super().execute(query, params, **kwargs)
    def measured_connect():
        conn = connect(); conn.cursor_factory = MeasuredCursor
        return conn
    monkeypatch.setattr(mod, '_conn', measured_connect)
    result = mod.events_page('r', 1000000, 500)
    assert result['events'] == []
    assert result['missing_sequence_ranges'] == []
    assert result['event_sequence_high_watermark'] == 1000000
    body = snapshot(1000002); body['event_only'] = True
    body['events'] = [event(1000002, 'EVIDENCE', position=None)]
    assert mod.ingest(body)['missing_sequence_ranges'] == [[1000001, 1000001]]
    def nodes(node):
        yield node
        for child in node.get('Plans', []):
            yield from nodes(child)
    for plan in plans:
        steps = list(nodes(plan['Plan']))
        assert not any(step['Node Type'] == 'WindowAgg' for step in steps)
        assert max(step.get('Actual Rows', 0) for step in steps) <= 1
        assert max(step.get('Rows Removed by Filter', 0) for step in steps) <= 1
        event_reads = [step for step in steps if step.get('Relation Name') == 'market_review_events']
        assert all('Index' in step['Node Type'] for step in event_reads)
    print('event_gap_index_probe: historical_events=1000000 max_query_rows=1 '
          'query_ms=' + ','.join(str(plan['Execution Time']) for plan in plans))
