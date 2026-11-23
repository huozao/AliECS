"""Personal WeChat uses the shared notification ledger and an executor lease."""
import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from Crypto.Cipher import AES
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.notify import store
from app.notify.models import NotifyImage


class Text(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=2000)


class Image(NotifyImage):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    type: Literal["image"]


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    event: str = Field(min_length=1, max_length=128)
    dedup_key: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=128)
    expires_at: datetime
    sensitive: bool = False
    content: list[Annotated[Text | Image, Field(discriminator="type")]] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_content(self):
        if self.expires_at.utcoffset() is None:
            raise ValueError("expires_at requires a timezone")
        if self.sensitive and (len(self.content) != 1 or not isinstance(self.content[0], Text)):
            raise ValueError("sensitive delivery must be one text part")
        if sum(len(part.png_base64) for part in self.content if isinstance(part, Image)) > 4_000_000:
            raise ValueError("aggregate image limit exceeded")
        return self


class Lease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease: str = Field(min_length=20, max_length=128)


class Receipt(Lease):
    step: int = Field(ge=0, lt=8)
    outcome: Literal["sent", "dead", "unknown"]
    message_id: str | None = Field(None, pattern=r"^[1-9][0-9]{0,39}$")

    @model_validator(mode="after")
    def validate_result(self):
        if (self.outcome == "sent") != bool(self.message_id):
            raise ValueError("sent requires a message ID")
        return self


def _key():
    try:
        key = base64.b64decode(os.environ["NOTIFY_WECHAT_PAYLOAD_KEY"], validate=True)
        if len(key) == 32:
            return key
    except (KeyError, ValueError):
        pass
    raise HTTPException(503, "wechat payload key unavailable")


def require_agent(agent, token):
    if not agent or not token:
        raise HTTPException(401, "invalid executor")
    try:
        tokens = json.loads(os.environ.get("NOTIFY_WECHAT_AGENT_TOKENS", "{}"))
    except ValueError:
        raise HTTPException(503, "executor configuration unavailable") from None
    expected = tokens.get(agent) if isinstance(tokens, dict) else None
    if not isinstance(expected, str) or not expected or not hmac.compare_digest(expected, token):
        raise HTTPException(401, "invalid executor")
    return agent


def _seal(key, identifier, payload):
    cipher = AES.new(key, AES.MODE_GCM)
    cipher.update(str(identifier).encode())
    ciphertext, tag = cipher.encrypt_and_digest(payload)
    return cipher.nonce + tag + ciphertext


def _open(key, identifier, data):
    data = bytes(data)
    cipher = AES.new(key, AES.MODE_GCM, nonce=data[:16])
    cipher.update(str(identifier).encode())
    return json.loads(cipher.decrypt_and_verify(data[32:], data[16:32]))


def _target_options(conn, source, body):
    routes = store.matching_routes(conn, source, body.event, "info", channel="personal_wechat")
    matches = []
    for route in routes:
        target = route["target_json"]
        if (route["source_key"] == source and target.get("alias") == body.target
                and isinstance(target.get("agent"), str) and target["agent"]
                and isinstance(target.get("username"), str) and target["username"]
                and (not body.sensitive or target.get("sensitive") is True)):
            matches.append(route)
    return matches


def submit(conn, source, body):
    key = _key()
    serialized = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hmac.new(key, serialized, hashlib.sha256).hexdigest()
    dedup = "personal-wechat:" + hashlib.sha256(json.dumps([source, body.dedup_key]).encode()).hexdigest()
    with conn.cursor() as cur:
        def existing():
            cur.execute("""
                SELECT o.id, d.id, d.status, p.fingerprint
                FROM notify_outbox o JOIN notify_deliveries d ON d.outbox_id=o.id
                JOIN notify_wechat_payloads p ON p.delivery_id=d.id
                WHERE o.dedup_key=%s AND o.source_key=%s
            """, (dedup, source))
            row = cur.fetchone()
            if row and not hmac.compare_digest(row[3], fingerprint):
                raise HTTPException(409, "dedup key conflicts with an existing notification")
            return row

        row = existing()
        if row:
            return {"outbox_id": row[0], "delivery_id": row[1], "status": row[2], "duplicate": True}
        now = datetime.now(timezone.utc)
        limit = timedelta(minutes=30) if body.sensitive else timedelta(hours=24)
        if not now < body.expires_at <= now + limit:
            raise HTTPException(422, "invalid delivery validity window")
        routes = _target_options(conn, source, body)
        if len(routes) != 1:
            raise HTTPException(403, "target route missing or ambiguous")
        cur.execute("""
            INSERT INTO notify_outbox(dedup_key,source_key,event,level,payload_json)
            VALUES (%s,%s,%s,'info',%s)
            ON CONFLICT (dedup_key) DO NOTHING RETURNING id
        """, (dedup, source, body.event, Jsonb({"channel":"personal_wechat","sensitive":body.sensitive})))
        new = cur.fetchone()
        if not new:
            row = existing()
            if not row:
                raise HTTPException(409, "dedup key unavailable")
            return {"outbox_id": row[0], "delivery_id": row[1], "status": row[2], "duplicate": True}
        outbox_id = new[0]
        delivery = store.create_deliveries(conn, outbox_id, routes)[0]
        delivery_id = delivery["id"]
        cur.execute("UPDATE notify_deliveries SET status='wechat_pending' WHERE id=%s", (delivery_id,))
        cur.execute("""
            INSERT INTO notify_wechat_payloads(delivery_id,agent,ciphertext,fingerprint,expires_at,step_count)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (delivery_id, routes[0]["target_json"]["agent"], _seal(key, delivery_id, serialized),
              fingerprint, body.expires_at, len(body.content)))
        return {"outbox_id": outbox_id, "delivery_id": delivery_id, "status": "wechat_pending", "duplicate": False}


def cleanup(conn):
    """Expire queued jobs and hold uncertain external sends for reconciliation."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH eligible AS (
                SELECT d.id, CASE
                    WHEN d.status='wechat_sending' THEN 'unknown'
                    WHEN p.expires_at<=now() THEN 'expired'
                    WHEN NOT coalesce(s.enabled AND r.enabled AND r.channel=d.channel AND r.target_json=d.target_json,false)
                         THEN 'cancelled'
                    ELSE 'wechat_pending' END AS new_status
                FROM notify_deliveries d JOIN notify_wechat_payloads p ON p.delivery_id=d.id
                JOIN notify_outbox o ON o.id=d.outbox_id
                LEFT JOIN notify_sources s ON s.source_key=o.source_key
                LEFT JOIN notify_routes r ON r.id=d.route_id
                WHERE d.status IN ('wechat_pending','wechat_leased','wechat_sending')
                  AND (p.expires_at<=now() OR p.lease_until<=now()
                    OR (d.status!='wechat_sending' AND
                        NOT coalesce(s.enabled AND r.enabled AND r.channel=d.channel AND r.target_json=d.target_json,false)))
                FOR UPDATE OF d SKIP LOCKED
            ), updated AS (
                UPDATE notify_deliveries d SET status=e.new_status FROM eligible e WHERE d.id=e.id
                RETURNING d.id,d.status
            )
            UPDATE notify_wechat_payloads p
            SET ciphertext=CASE WHEN u.status='wechat_pending' THEN p.ciphertext ELSE NULL END,
                lease=NULL,lease_until=NULL
            FROM updated u WHERE p.delivery_id=u.id
        """)


def claim(conn, agent):
    cleanup(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.id FROM notify_deliveries d JOIN notify_wechat_payloads p ON d.id=p.delivery_id
            JOIN notify_outbox o ON o.id=d.outbox_id
            JOIN notify_sources s ON s.source_key=o.source_key AND s.enabled
            JOIN notify_routes r ON r.id=d.route_id AND r.enabled AND r.channel=d.channel
                AND r.target_json=d.target_json
            WHERE d.status='wechat_pending' AND p.agent=%s AND p.expires_at>now()
            ORDER BY p.expires_at,d.id LIMIT 1 FOR UPDATE OF d SKIP LOCKED
        """, (agent,))
        row = cur.fetchone()
        if not row:
            return None
        delivery_id = row[0]
        lease = secrets.token_urlsafe(32)
        cur.execute("UPDATE notify_deliveries SET status='wechat_leased' WHERE id=%s", (delivery_id,))
        cur.execute("""
            UPDATE notify_wechat_payloads SET lease=%s,lease_until=now()+interval '90 seconds'
            WHERE delivery_id=%s
        """, (lease, delivery_id))
        return {"delivery_id": delivery_id, "lease": lease}


def begin(conn, agent, delivery_id, lease):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.ciphertext,p.next_step,p.expires_at,d.target_json,EXTRACT(EPOCH FROM now())
            FROM notify_deliveries d JOIN notify_wechat_payloads p ON d.id=p.delivery_id
            JOIN notify_outbox o ON o.id=d.outbox_id
            JOIN notify_sources s ON s.source_key=o.source_key AND s.enabled
            JOIN notify_routes r ON r.id=d.route_id AND r.enabled AND r.channel=d.channel
                AND r.target_json=d.target_json
            WHERE d.id=%s AND p.agent=%s AND p.lease=%s AND d.status='wechat_leased'
              AND p.lease_until>now() AND p.expires_at>now()
            FOR UPDATE OF d
        """, (delivery_id, agent, lease))
        row = cur.fetchone()
        if not row:
            raise HTTPException(409, "delivery lease or route unavailable")
        payload = _open(_key(), delivery_id, row[0])
        content = payload["content"][row[1]]
        cur.execute("""
            UPDATE notify_deliveries SET status='wechat_sending',attempts=attempts+1
            WHERE id=%s
        """, (delivery_id,))
        cur.execute("""
            UPDATE notify_wechat_payloads SET lease_until=now()+interval '90 seconds'
            WHERE delivery_id=%s
        """, (delivery_id,))
        return {"step":row[1],"expires_at":row[2],"username":row[3]["username"],"content":content,
                "sensitive":payload["sensitive"],"started_at":int(row[4])}


def finish(conn, agent, delivery_id, receipt):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.status,p.next_step,p.step_count,p.receipts,p.lease_until>now()
            FROM notify_deliveries d JOIN notify_wechat_payloads p ON d.id=p.delivery_id
            WHERE d.id=%s AND p.agent=%s AND p.lease=%s FOR UPDATE OF d
        """, (delivery_id, agent, receipt.lease))
        row = cur.fetchone()
        if not row:
            raise HTTPException(409, "delivery lease unavailable")
        current, index, total, receipts, live = row
        marker = str(receipt.step)
        recorded = {"status": receipt.outcome, "message_id": receipt.message_id}
        if marker in receipts:
            if receipts[marker] != recorded:
                raise HTTPException(409, "conflicting receipt")
            return {"status": current}
        if current != "wechat_sending" or index != receipt.step or not live:
            raise HTTPException(409, "receipt does not match current step")
        status = receipt.outcome
        if status == "sent" and index+1 < total:
            status = "wechat_pending"
        receipts[marker] = recorded
        cur.execute("""
            UPDATE notify_deliveries SET status=%s,
                sent_at=CASE WHEN %s='sent' THEN now() ELSE sent_at END
            WHERE id=%s
        """, (status, status, delivery_id))
        cur.execute("""
            UPDATE notify_wechat_payloads SET receipts=%s,next_step=%s,
                ciphertext=CASE WHEN %s='wechat_pending' THEN ciphertext ELSE NULL END
            WHERE delivery_id=%s
        """, (Jsonb(receipts), index+1, status, delivery_id))
        return {"status":status}
