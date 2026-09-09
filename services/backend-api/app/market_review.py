"""Bounded market evidence queries; account cashflows never use paper fills."""
from __future__ import annotations

import copy
import base64
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from app.core import _conn

SCHEMA = 'market-review.v1'
SERIES_PAGE_SIZE = 5000
GAP_PAGE_SIZE = 2000
TARGET_FILL_CODE = 'TARGET_FILL_CONFIRMED'


def utc(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError('timezone required')
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, 'expected ISO timestamp with timezone') from exc


def window(start: str | None, end: str | None, bucket_ms: int = 1000):
    right = utc(end) if end else datetime.now(timezone.utc)
    left = utc(start) if start else right - timedelta(minutes=15)
    if bucket_ms not in (1000, 5000, 10000, 60000) or not 0 < (right-left).total_seconds() <= 86400:
        raise HTTPException(422, 'window must be positive and at most one day; bucket_ms=1000|5000|10000|60000')
    return left, right


def sequence_gaps(sequences: list[int]) -> list[list[int]]:
    result, previous = [], 0
    for value in sorted(set(sequences)):
        if value > previous + 1:
            result.append([previous + 1, value - 1])
        previous = value
    return result


def _gap_summary(cur, run_id: str) -> dict:
    cur.execute('SELECT start_sequence,end_sequence FROM market_review_event_gaps WHERE run_id=%s ORDER BY start_sequence LIMIT %s',
                (run_id, GAP_PAGE_SIZE + 1))
    gaps = cur.fetchall()
    cur.execute('SELECT max_sequence FROM market_review_event_watermarks WHERE run_id=%s', (run_id,))
    watermark = cur.fetchone()
    return {'missing_sequence_ranges': [list(row) for row in gaps[:GAP_PAGE_SIZE]],
            'missing_sequence_ranges_truncated': len(gaps) > GAP_PAGE_SIZE,
            'event_sequence_high_watermark': watermark[0] if watermark else 0}


def project_position(events: list[dict]) -> dict:
    events = sorted(events, key=lambda e: e.get('sequence', 0))
    legs = {name: {'opened_volume': 0, 'closed_volume': 0, 'remaining_volume': 0,
                  'gross_profit_cny': None, 'fills': []} for name in ('target', 'hedge')}
    seen, reasons, decisions, model, actual_fees = set(), [], [], {}, Decimal(0)
    fee_known, any_trade = True, False
    for event in events:
        payload = event.get('payload', {})
        kind = event.get('event_type', '')
        if 'candidates' in payload:
            decisions.append(event)
        if kind == 'MODEL_RESULT':
            model = copy.deepcopy(payload)
        if kind != 'ACCOUNT_TRADE':
            if any(word in kind for word in ('UNRESOLVED', 'UNKNOWN', 'REJECT', 'RECONCILIATION_REQUIRED')):
                reasons.append({'event_id': event.get('event_id'), 'reason': payload.get('reason', kind)})
            continue
        key = (payload.get('account_id'), event.get('trading_day'), event.get('order_id'), event.get('trade_id'))
        if key in seen:
            continue
        seen.add(key)
        name = payload.get('leg')
        if name not in legs or not key[0] or not key[3]:
            reasons.append({'event_id': event.get('event_id'), 'reason': '账户成交标识或腿缺失'})
            continue
        any_trade = True
        leg = legs[name]
        leg['fills'].append(event)
        volume = Decimal(str(payload['volume']))
        field = 'opened_volume' if payload['offset'] == 'OPEN' else 'closed_volume'
        leg[field] += float(volume)
        leg['remaining_volume'] = leg['opened_volume'] - leg['closed_volume']
        if payload.get('commission_cny') is None:
            fee_known = False
        else:
            actual_fees += Decimal(str(payload['commission_cny']))
    complete = any_trade and all(l['opened_volume'] > 0 and l['remaining_volume'] == 0 for l in legs.values())
    for name, leg in legs.items():
        if leg['opened_volume'] > 0 and leg['remaining_volume'] == 0:
            cash = Decimal(0)
            for e in leg['fills']:
                p = e['payload']
                cash += (1 if p['direction'] == 'SELL' else -1) * Decimal(str(p['price'])) * Decimal(str(p['volume'])) * Decimal(str(p['multiplier']))
            leg['gross_profit_cny'] = float(cash)
        if leg['remaining_volume'] != 0:
            reasons.append({'reason': f'{name}｜{"目标腿" if name == "target" else "对冲腿"}剩余 {leg["remaining_volume"]} 手'})
    if not any_trade:
        reasons.append({'reason': '无账户成交测量，不能以模型成交补齐'})
    gross = sum(l['gross_profit_cny'] for l in legs.values()) if complete else None
    fixed = model.get('fixed_cost_cny')
    return {'position_id': next((e.get('position_id') for e in events if e.get('position_id')), None),
            'run_id': next((e.get('run_id') for e in events if e.get('run_id')), None),
            'model': model, 'account': {'status': 'CLOSED' if complete else 'UNRESOLVED', 'legs': legs,
                'gross_profit_cny': gross, 'fixed_cost_cny': fixed,
                'fixed_cost_net_cny': gross - fixed if gross is not None and fixed is not None else None,
                'actual_commission_cny': float(actual_fees) if fee_known and any_trade else None,
                'actual_net_cny': gross - float(actual_fees) if gross is not None and fee_known else None},
            'unresolved': not complete, 'reasons': reasons, 'decisions': decisions, 'events': events}


def validate_snapshot(body: dict) -> dict:
    if body.get('schema_version') != SCHEMA or body.get('model_version') != 'V6.0':
        raise HTTPException(422, 'unsupported market review schema/model')
    if not isinstance(body.get('run_id'), str) or not body['run_id'] or len(body['run_id']) > 200:
        raise HTTPException(422, 'invalid run_id')
    if type(body.get('sequence')) is not int or body['sequence'] < 0:
        raise HTTPException(422, 'invalid sequence')
    utc(body.get('published_at'))
    for field in ('quotes', 'bands', 'events'):
        if not isinstance(body.get(field), list) or len(body[field]) > 2000:
            raise HTTPException(422, f'invalid {field}')
    for field in ('quotes', 'bands'):
        for item in body[field]:
            if not isinstance(item, dict):
                raise HTTPException(422, f'invalid {field} observation')
            # Unknown source time is valid, malformed timestamps are not. An
            # invalid JSON timestamp must not poison later SQL window queries.
            for key in ('source_time', 'captured_at', 'observed_at'):
                if item.get(key) is not None:
                    utc(item[key])
    for event in body['events']:
        required = ('event_id','run_id','sequence','trading_day','model_version','world_id','position_id','order_id','trade_id','event_type','occurred_at','recorded_at','payload')
        if not isinstance(event, dict) or any(k not in event for k in required):
            raise HTTPException(422, 'incomplete event envelope')
        if event['run_id'] != body['run_id'] or type(event['sequence']) is not int or event['sequence'] < 1:
            raise HTTPException(422, 'invalid event run/sequence')
        if not event['event_id'] or not isinstance(event['payload'], dict):
            raise HTTPException(422, 'invalid event id/payload')
        utc(event['occurred_at']); utc(event['recorded_at'])
        if event['event_type'] == 'ACCOUNT_TRADE':
            p = event['payload']
            if p.get('leg') not in ('target','hedge') or p.get('offset') not in ('OPEN','CLOSE') or p.get('direction') not in ('BUY','SELL'):
                raise HTTPException(422, 'invalid account trade leg/offset/direction')
            for key in ('price','volume','multiplier'):
                if not isinstance(p.get(key), (int,float)) or isinstance(p[key],bool) or (key != 'price' and p[key] <= 0):
                    raise HTTPException(422, 'invalid account trade number')
    try:
        json.dumps(body, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, 'nonfinite/invalid JSON value') from exc
    result = copy.deepcopy(body)
    result.pop('received_at', None)
    return result


def ingest(body: dict) -> dict:
    body = validate_snapshot(body)
    event_only = body.pop('event_only', False)
    if not isinstance(event_only, bool):
        raise HTTPException(422, 'event_only must be boolean')
    events = body.pop('events')
    with _conn() as conn, conn.cursor() as cur:
        # One run is an ordered stream. Lock serializes retries and projection updates.
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('market:' + body['run_id'],))
        if event_only:
            # Snapshot and event sequences are independent streams. Event-only
            # retries must not claim a snapshot primary key or erase the latest
            # chart with an empty quote list.
            cur.execute('SELECT clock_timestamp()')
            received = cur.fetchone()[0]
        else:
            cur.execute('INSERT INTO market_review_snapshots(run_id,sequence,published_at,body) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                        (body['run_id'], body['sequence'], utc(body['published_at']), Jsonb(body)))
            cur.execute('SELECT body, received_at FROM market_review_snapshots WHERE run_id=%s AND sequence=%s', (body['run_id'],body['sequence']))
            existing, received = cur.fetchone()
            if existing != body:
                raise HTTPException(409, 'snapshot identity conflicts with immutable evidence')
            # Migration 0059 projects observations with an INSERT trigger in
            # this same transaction, including writes from older backend builds.
        touched = set()
        for event in events:
            cur.execute('INSERT INTO market_review_events(event_id,run_id,sequence,position_id,occurred_at,body) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                        (event['event_id'],event['run_id'],event['sequence'],event.get('position_id'),utc(event['occurred_at']),Jsonb(event)))
            cur.execute('SELECT body FROM market_review_events WHERE event_id=%s', (event['event_id'],))
            stored = cur.fetchone()
            if stored is None or stored[0] != event:
                raise HTTPException(409, 'event identity conflicts with immutable evidence')
            if event.get('position_id'):
                touched.add(event['position_id'])
        for position in touched:
            cur.execute('SELECT body FROM market_review_events WHERE run_id=%s AND position_id=%s ORDER BY sequence LIMIT 20001', (body['run_id'],position))
            history = [row[0] for row in cur.fetchall()]
            if len(history) > 20000:
                raise HTTPException(413, 'position exceeds evidence limit; archive review required')
            view = project_position(history)
            cur.execute('INSERT INTO market_review_positions(run_id,position_id,body,unresolved) VALUES(%s,%s,%s,%s) ON CONFLICT(run_id,position_id) DO UPDATE SET body=EXCLUDED.body,unresolved=EXCLUDED.unresolved,updated_at=clock_timestamp()',
                        (body['run_id'], position, Jsonb(view),view['unresolved']))
        gaps = _gap_summary(cur, body['run_id'])
    return {'ok': True, 'run_id': body['run_id'], 'sequence': body['sequence'], 'received_at': received.isoformat(),
            'ack_event_ids': [e['event_id'] for e in events], **gaps}


def latest() -> dict | None:
    with _conn() as conn, conn.cursor() as cur:
        # Event-only 补发包只承载不可丢失的交易事件，不能把最新行情覆盖成空图。
        # 事件仍在 market_review_events 中按序保存；最新行情只从有快照的包读取。
        cur.execute("SELECT body,received_at FROM market_review_snapshots WHERE COALESCE(body->>'event_only','false') <> 'true' ORDER BY published_at DESC,sequence DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        result = dict(row[0]); result['received_at'] = row[1].isoformat()
        # Select each contract independently: a quiet contract must not disappear
        # after 200 updates of a busy one. Source time cannot regress; independent
        # international/model refreshes at the same source time remain visible.
        for field in ('quotes', 'bands'):
            cur.execute("""SELECT body,sequence FROM market_review_current_observations
                WHERE run_id=%s AND field=%s ORDER BY contract""", (result['run_id'], field))
            result[field] = [dict(item, observed_at=item.get('observed_at') or item.get('captured_at') or item.get('source_time'),
                                  snapshot_sequence=seq, run_id=result['run_id']) for item, seq in cur.fetchall()]
        cur.execute('SELECT body FROM market_review_positions WHERE unresolved ORDER BY updated_at DESC LIMIT 201')
        unresolved = [r[0] for r in cur.fetchall()]
        result['unresolved_positions'] = unresolved[:200]
        result['unresolved_has_more'] = len(unresolved) > 200
        return result


def events_page(run_id: str, after: int, limit: int) -> dict:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT body FROM market_review_events WHERE run_id=%s AND sequence>%s ORDER BY sequence LIMIT %s', (run_id, after, limit+1))
        rows = [r[0] for r in cur.fetchall()]
        page = rows[:limit]
        gaps = _gap_summary(cur, run_id)
    return {'events': page, 'run_id': run_id, 'next_sequence': page[-1]['sequence'] if page else after,
            'has_more': len(rows)>limit, **gaps}


def alert_state(run_id: str, user_id: int, after_sequence: int = 0, limit: int = 200) -> dict:
    """Return target-fill reminders and this user's read state.

    The event stream remains the source of truth.  This table only stores a
    per-user acknowledgement and therefore cannot mark a position closed or
    remove an unresolved exposure.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        raise HTTPException(422, 'invalid user identity')
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT e.body, (r.event_id IS NOT NULL)
               FROM market_review_events e
               LEFT JOIN market_review_event_reads r
                 ON r.event_id=e.event_id AND r.user_id=%s
              WHERE e.run_id=%s AND e.sequence>%s
                AND split_part(e.body->>'event_type','｜',1)=%s
              ORDER BY e.sequence LIMIT %s""",
            (user_id, run_id, after_sequence, TARGET_FILL_CODE, limit + 1),
        )
        rows = cur.fetchall()
    page = rows[:limit]
    return {
        'run_id': run_id,
        'alerts': [row[0] for row in page],
        'read_event_ids': [row[0]['event_id'] for row in page if row[1]],
        'has_more': len(rows) > limit,
        'next_sequence': page[-1][0]['sequence'] if page else after_sequence,
    }


def mark_alert_read(run_id: str, event_id: str, user_id: int) -> dict:
    if not isinstance(user_id, int) or user_id <= 0:
        raise HTTPException(422, 'invalid user identity')
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM market_review_events WHERE run_id=%s AND event_id=%s AND split_part(body->>'event_type','｜',1)=%s",
            (run_id, event_id, TARGET_FILL_CODE),
        )
        if cur.fetchone() is None:
            raise HTTPException(404, 'target fill alert not found')
        cur.execute(
            """INSERT INTO market_review_event_reads(user_id,run_id,event_id)
               VALUES(%s,%s,%s) ON CONFLICT(user_id,event_id) DO NOTHING
               RETURNING read_at""",
            (user_id, run_id, event_id),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT read_at FROM market_review_event_reads WHERE user_id=%s AND event_id=%s",
                (user_id, event_id),
            )
            row = cur.fetchone()
    return {'ok': True, 'run_id': run_id, 'event_id': event_id, 'read_at': row[0].isoformat()}


def position_view(position_id: str, run_id: str) -> dict:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT body FROM market_review_positions WHERE run_id=%s AND position_id=%s', (run_id,position_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, 'position not found')
        return row[0]


def series(symbol: str, start: str | None, end: str | None, bucket_ms: int, after: str | None = None, run_id: str | None = None) -> dict:
    left,right = window(start,end,bucket_ms)
    clauses = ['observed_at >= %s', 'observed_at < %s']
    params: list = [left, right]
    if run_id:
        clauses.append('run_id=%s')
        params.append(run_id)
    if after:
        if after.startswith('v2.'):
            try:
                stamp, cursor_run, seq, field, ordinal = json.loads(base64.urlsafe_b64decode(after[3:] + '=' * (-len(after[3:]) % 4)))
                if not isinstance(cursor_run, str) or type(seq) is not int or field not in ('quotes', 'bands') or type(ordinal) is not int or ordinal < 1:
                    raise ValueError('invalid cursor fields')
                if run_id and cursor_run != run_id:
                    raise ValueError('cursor belongs to another run')
                clauses.append('(observed_at,run_id,sequence,field,ordinal) > (%s,%s,%s,%s,%s)')
                params.extend([utc(stamp), cursor_run, seq, field, ordinal])
            except (ValueError, TypeError, UnicodeError) as exc:
                raise HTTPException(422, 'invalid series cursor') from exc
        elif after.startswith('v1.'):
            try:
                stamp, cursor_run, seq = json.loads(base64.urlsafe_b64decode(after[3:] + '=' * (-len(after[3:]) % 4)))
                if not isinstance(cursor_run, str) or type(seq) is not int:
                    raise ValueError('invalid cursor fields')
                clauses.append('(published_at,run_id,sequence) > (%s,%s,%s)')
                params.extend([utc(stamp), cursor_run, seq])
            except (ValueError, TypeError, UnicodeError) as exc:
                raise HTTPException(422, 'invalid series cursor') from exc
        else:
            clauses.append('published_at > %s')
            params.append(utc(after))
    if symbol != '*':
        clauses.append('contract=%s')
        params.append(symbol)
    # Page by observation identity, not domestic source seconds or publication.
    # Late uploads remain queryable at their actual observation time. SQL limits
    # returned observation rows even when a packet contains many contracts.
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT body,run_id,sequence,field,ordinal,observed_at
            FROM market_review_observations WHERE """ + ' AND '.join(clauses) +
            ' ORDER BY observed_at,run_id,sequence,field,ordinal LIMIT %s', (*params, SERIES_PAGE_SIZE + 1))
        rows = cur.fetchall()
    page = rows[:SERIES_PAGE_SIZE]
    fields = {'quotes': [], 'bands': []}
    for item, row_run, seq, field, ordinal, observed_at in page:
        fields[field].append(dict(item, run_id=row_run, snapshot_sequence=seq,
                                  observed_at=observed_at.isoformat()))
    cursor = after
    if page:
        last = page[-1]
        cursor = 'v2.' + base64.urlsafe_b64encode(json.dumps([last[5].isoformat(), last[1], last[2], last[3], last[4]]).encode()).decode().rstrip('=')
    return {'symbol':symbol,'start':left.isoformat(),'end':right.isoformat(),'bucket_ms':bucket_ms,
            'quotes':fields['quotes'],'bands':fields['bands'],'has_more':len(rows)>SERIES_PAGE_SIZE,
            'next_after':cursor,
            'aggregation': 'source_ohlc_preserved', 'time_basis': 'observed_at'}


def comparison(symbol: str, start: str | None, end: str | None, run_id: str | None = None) -> dict:
    left,right = window(start,end)
    with _conn() as conn, conn.cursor() as cur:
        clauses = ["published_at >= %s", "published_at <= %s", "body->'comparison'->>'symbol'=%s", "body->'comparison'->>'start'=%s", "body->'comparison'->>'end'=%s"]
        params: list = [left, right, symbol, start, end]
        if run_id:
            clauses.append('run_id=%s')
            params.append(run_id)
        cur.execute("SELECT body->'comparison' FROM market_review_snapshots WHERE " + ' AND '.join(clauses) + ' ORDER BY published_at DESC LIMIT 1', params)
        row=cur.fetchone()
    return row[0] if row and row[0] else {'available':False,'status':'NOT_RUN','symbol':symbol,'start':left.isoformat(),'end':right.isoformat()}


def annotations(position_id: str, run_id: str, after_id: int = 0) -> dict:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT id,revision,author_id,author_name,verdict,reason,evidence_ids,created_at FROM market_review_annotations WHERE run_id=%s AND position_id=%s AND id>%s ORDER BY id LIMIT 501', (run_id,position_id,after_id))
        rows=cur.fetchall()
    keys=('id','revision','author_id','author_name','verdict','reason','evidence_ids','created_at')
    items=[dict(zip(keys, row)) for row in rows[:500]]
    for item in items:
        item['created_at']=item['created_at'].isoformat()
    return {'annotations':items,'has_more':len(rows)>500,'next_id':items[-1]['id'] if items else after_id}


def annotate(body: dict, user: dict) -> dict:
    allowed={'position_id','run_id','verdict','reason','evidence_ids'}
    if set(body)-allowed or body.get('verdict') not in ('confirmed','uncertain','excluded'):
        raise HTTPException(422,'invalid annotation; identity is supplied by server session')
    if not all(isinstance(body.get(k),str) and body[k].strip() for k in ('position_id','run_id','reason')) or len(body['reason'])>4000:
        raise HTTPException(422,'position/run/reason required, reason <=4000 characters')
    refs=body.get('evidence_ids',[])
    if not isinstance(refs,list) or len(refs)>100 or any(not isinstance(r,str) or len(r)>200 for r in refs):
        raise HTTPException(422,'invalid evidence references')
    position_view(body['position_id'],body['run_id'])
    with _conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('annotation:'+body['run_id']+':'+body['position_id'],))
        cur.execute('SELECT COALESCE(MAX(revision),0)+1 FROM market_review_annotations WHERE run_id=%s AND position_id=%s', (body['run_id'],body['position_id']))
        revision=cur.fetchone()[0]
        cur.execute('INSERT INTO market_review_annotations(position_id,run_id,revision,author_id,author_name,verdict,reason,evidence_ids) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,created_at',
            (body['position_id'],body['run_id'],revision,user['id'],user.get('username') or user.get('display_name') or str(user['id']),body['verdict'],body['reason'],Jsonb(refs)))
        identity,created=cur.fetchone()
    return {'id':identity,'revision':revision,'created_at':created.isoformat()}
