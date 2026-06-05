from __future__ import annotations

import os
from contextlib import closing
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.integrations.chanjet.schemas import ChanjetEvent
from app.integrations.events import build_chanjet_bom_sync_request, stable_json_hash


def connect_if_configured() -> psycopg.Connection | None:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return None
    return psycopg.connect(database_url, connect_timeout=3)


def save_chanjet_event_and_queue_request(
    conn: Any,
    event: ChanjetEvent,
    record: dict[str, Any],
) -> dict[str, int | None]:
    payload_hash = stable_json_hash(record)
    normalized_json = {
        "provider": "chanjet",
        "event_type": event.msg_type,
        "event_id": event.event_id,
        "app_key": event.app_key,
        "app_id": event.app_id,
        "received_time": event.received_time,
        "biz_content": event.biz_content,
    }

    with conn.cursor() as cur:
        if event.event_id:
            cur.execute(
                """
                INSERT INTO integration_events(
                    provider, event_type, event_id, status, raw_json, normalized_json, payload_hash
                )
                VALUES (%s, %s, %s, 'received', %s, %s, %s)
                ON CONFLICT (provider, event_id) WHERE event_id IS NOT NULL
                DO UPDATE SET
                    event_type = EXCLUDED.event_type,
                    raw_json = EXCLUDED.raw_json,
                    normalized_json = EXCLUDED.normalized_json,
                    payload_hash = EXCLUDED.payload_hash,
                    updated_at = NOW()
                RETURNING id
                """,
                ("chanjet", event.msg_type, event.event_id, Jsonb(record), Jsonb(normalized_json), payload_hash),
            )
        else:
            cur.execute(
                """
                INSERT INTO integration_events(
                    provider, event_type, event_id, status, raw_json, normalized_json, payload_hash
                )
                VALUES (%s, %s, %s, 'received', %s, %s, %s)
                RETURNING id
                """,
                ("chanjet", event.msg_type, None, Jsonb(record), Jsonb(normalized_json), payload_hash),
            )
        event_row_id = int(cur.fetchone()[0])

        sync_request_id: int | None = None
        request = build_chanjet_bom_sync_request(event)
        if request is not None:
            cur.execute(
                """
                INSERT INTO integration_sync_requests(
                    provider, module, mode, target_json, reason_event_id, priority, status, dedupe_key
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
                ON CONFLICT (dedupe_key)
                DO UPDATE SET
                    target_json = EXCLUDED.target_json,
                    reason_event_id = EXCLUDED.reason_event_id,
                    priority = LEAST(integration_sync_requests.priority, EXCLUDED.priority),
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    request["provider"],
                    request["module"],
                    request["mode"],
                    Jsonb(request["target_json"]),
                    request["reason_event_id"],
                    request["priority"],
                    request["dedupe_key"],
                ),
            )
            sync_request_id = int(cur.fetchone()[0])
    conn.commit()
    return {"event_id": event_row_id, "sync_request_id": sync_request_id}


def save_chanjet_event_with_configured_database(event: ChanjetEvent, record: dict[str, Any]) -> dict[str, int | None] | None:
    conn = connect_if_configured()
    if conn is None:
        return None
    with closing(conn):
        return save_chanjet_event_and_queue_request(conn, event, record)
