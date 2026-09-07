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
        # LAG reports ranges, never materializes potentially enormous missing ID lists.
        cur.execute('SELECT previous+1, sequence-1 FROM (SELECT sequence,lag(sequence,1,0) OVER(ORDER BY sequence) previous FROM market_review_events WHERE run_id=%s) s WHERE sequence>previous+1 LIMIT 2000', (body['run_id'],))
        gaps = [list(row) for row in cur.fetchall()]
    return {'ok': True, 'run_id': body['run_id'], 'sequence': body['sequence'], 'received_at': received.isoformat(),
            'ack_event_ids': [e['event_id'] for e in events], 'missing_sequence_ranges': gaps}


def latest() -> dict | None:
    with _conn() as conn, conn.cursor() as cur:
        # Event-only 补发包只承载不可丢失的交易事件，不能把最新行情覆盖成空图。
        # 事件仍在 market_review_events 中按序保存；最新行情只从有快照的包读取。
        cur.execute("SELECT body,received_at FROM market_review_snapshots WHERE COALESCE(body->>'event_only','false') <> 'true' ORDER BY published_at DESC,sequence DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        result = dict(row[0]); result['received_at'] = row[1].isoformat()
        # Incremental packets carry only changed buckets. Retain the most recent
        # observation for each contract, without expanding the entire history.
        cur.execute('SELECT body FROM market_review_snapshots WHERE run_id=%s ORDER BY published_at DESC,sequence DESC LIMIT 200', (result['run_id'],))
        packets = [r[0] for r in cur.fetchall()]
        for field in ('quotes', 'bands'):
            by_contract = {}
            for packet in packets:
                for item in packet.get(field, []):
                    contract = item.get('contract')
                    old = by_contract.get(contract)
                    item_time = utc(item['source_time']) if item.get('source_time') else datetime.min.replace(tzinfo=timezone.utc)
                    old_time = utc(old['source_time']) if old and old.get('source_time') else datetime.min.replace(tzinfo=timezone.utc)
                    if old is None or item_time > old_time:
                        by_contract[contract] = item
            result[field] = list(by_contract.values())
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
        cur.execute('SELECT previous+1,sequence-1 FROM (SELECT sequence,lag(sequence,1,0) OVER(ORDER BY sequence) previous FROM market_review_events WHERE run_id=%s) s WHERE sequence>previous+1 LIMIT 2000', (run_id,))
        gaps = [list(r) for r in cur.fetchall()]
    return {'events': page, 'run_id': run_id, 'next_sequence': page[-1]['sequence'] if page else after,
            'has_more': len(rows)>limit, 'missing_sequence_ranges': gaps}


def position_view(position_id: str, run_id: str) -> dict:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT body FROM market_review_positions WHERE run_id=%s AND position_id=%s', (run_id,position_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, 'position not found')
        return row[0]


def series(symbol: str, start: str | None, end: str | None, bucket_ms: int, after: str | None = None, run_id: str | None = None) -> dict:
    left,right = window(start,end,bucket_ms)
    clauses = ['published_at >= %s', 'published_at < %s']
    params: list = [left, right]
    if run_id:
        clauses.append('run_id=%s')
        params.append(run_id)
    if after:
        if after.startswith('v1.'):
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
    # Bounded page indexed by publication time. Quotes retain their own source times.
    with _conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT body FROM market_review_snapshots WHERE ' + ' AND '.join(clauses) + ' ORDER BY published_at,run_id,sequence LIMIT %s', (*params, SERIES_PAGE_SIZE + 1))
        rows = [r[0] for r in cur.fetchall()]
    page = rows[:SERIES_PAGE_SIZE]
    fields = {'quotes': {}, 'bands': {}}
    for snapshot in page:
        for field in fields:
            for item in snapshot[field]:
                if item.get('contract') == symbol:
                    bucket = item.get('ohlc', {}).get('bucket_start') or item['source_time']
                    key = (snapshot['run_id'], symbol, utc(bucket).replace(microsecond=0))
                    fields[field][key] = dict(item, run_id=snapshot['run_id'])
    cursor = after
    if page:
        last = page[-1]
        cursor = 'v1.' + base64.urlsafe_b64encode(json.dumps([last['published_at'], last['run_id'], last['sequence']]).encode()).decode().rstrip('=')
    return {'symbol':symbol,'start':left.isoformat(),'end':right.isoformat(),'bucket_ms':bucket_ms,
            'quotes':list(fields['quotes'].values()),'bands':list(fields['bands'].values()),'has_more':len(rows)>SERIES_PAGE_SIZE,
            'next_after':cursor,
            'aggregation': 'source_ohlc_preserved'}


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
