from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from app.integrations.feishu.crypto import decrypt_encrypted_payload, verify_signature
from app.integrations.feishu.schemas import FeishuEvent, parse_feishu_event


LOGGER = logging.getLogger(__name__)
MESSAGE_RECEIVE_EVENT_TYPE = "im.message.receive_v1"


class FeishuChallengeResponse(Exception):
    """Raised internally so the router can return the Feishu URL challenge body."""

    def __init__(self, challenge: str) -> None:
        super().__init__("feishu_url_challenge")
        self.challenge = challenge


def decode_feishu_payload(
    body: bytes,
    headers: dict[str, str],
    *,
    encrypt_key: str = "",
    verification_token: str = "",
    require_signature: bool = False,
) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Feishu webhook body is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Feishu webhook payload must be a JSON object")

    if "encrypt" in payload:
        if not encrypt_key:
            raise RuntimeError("FEISHU_ENCRYPT_KEY is required to decode encrypted payload")
        if require_signature:
            timestamp = str(headers.get("x-lark-request-timestamp") or "")
            nonce = str(headers.get("x-lark-request-nonce") or "")
            signature = str(headers.get("x-lark-signature") or "")
            if not verify_signature(
                timestamp=timestamp,
                nonce=nonce,
                signature=signature,
                encrypt_key=encrypt_key,
                body=body,
            ):
                raise PermissionError("Feishu webhook signature verification failed")
        payload = decrypt_encrypted_payload(str(payload["encrypt"]), encrypt_key)

    if verification_token:
        token = payload.get("token")
        if not token:
            header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
            token = header.get("token")
        if token and str(token) != verification_token:
            raise PermissionError("Feishu webhook verification token mismatch")

    return payload


def maybe_challenge_response(payload: dict[str, Any]) -> dict[str, str] | None:
    if payload.get("type") == "url_verification":
        challenge = str(payload.get("challenge") or "")
        return {"challenge": challenge}
    return None


def handle_feishu_webhook(
    body: bytes,
    headers: dict[str, str],
    *,
    event_sink: Callable[[FeishuEvent, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    encrypt_key = _env("FEISHU_WEBHOOK_ENCRYPT_KEY") or _env("FEISHU_ENCRYPT_KEY")
    verification_token = _env("FEISHU_WEBHOOK_VERIFICATION_TOKEN") or _env("FEISHU_VERIFICATION_TOKEN")
    require_signature = _truthy(os.getenv("FEISHU_WEBHOOK_REQUIRE_SIGNATURE"))

    payload = decode_feishu_payload(
        body,
        headers,
        encrypt_key=encrypt_key,
        verification_token=verification_token,
        require_signature=require_signature,
    )

    challenge = maybe_challenge_response(payload)
    if challenge is not None:
        spool_feishu_record("url-verification", {"challenge": challenge["challenge"]})
        return challenge

    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_type = str(header.get("event_type") or "")

    if event_type == MESSAGE_RECEIVE_EVENT_TYPE:
        event = parse_feishu_event(payload)
        record = _event_to_record(event)
        spool_feishu_record("event", record)
        if event_sink is not None:
            event_sink(event, record)
        LOGGER.info(
            "feishu webhook received event_type=%s event_id=%s chat_type=%s",
            event.event_type,
            event.event_id,
            event.chat_type,
        )
    else:
        spool_feishu_record(
            "event-other",
            {"event_type": event_type, "event_id": str(header.get("event_id") or "")},
        )
        LOGGER.info("feishu webhook received non-message event_type=%s", event_type)

    return {"status": "ok"}


def spool_feishu_record(record_type: str, record: dict[str, Any]) -> None:
    spool_dir = os.getenv("FEISHU_EVENT_SPOOL_DIR", "/tmp/aliecs-integration-events/feishu").strip()
    if not spool_dir:
        return
    os.makedirs(spool_dir, exist_ok=True)
    file_name = f"{int(time.time() * 1000)}-{record_type}.json"
    path = os.path.join(spool_dir, file_name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)


def _event_to_record(event: FeishuEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "tenant_key": event.tenant_key,
        "create_time": event.create_time,
        "schema": event.schema_version,
        "chat_id": event.chat_id,
        "chat_type": event.chat_type,
        "message_id": event.message_id,
        "message_type": event.message_type,
        "sender_open_id": event.sender_open_id,
        "sender_user_id": event.sender_user_id,
        "sender_name": event.sender_name,
        "is_group": event.is_group_message,
        "mentions": [
            {"open_id": m.open_id, "user_id": m.user_id, "name": m.name}
            for m in event.mentions
        ],
        "content": event.content,
    }


def _env(name: str) -> str:
    raw = os.getenv(name, "")
    return raw.strip() if raw else ""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}
