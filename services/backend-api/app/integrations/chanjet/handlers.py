from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from app.integrations.chanjet.crypto import decrypt_encrypt_msg
from app.integrations.chanjet.schemas import ChanjetEvent, parse_chanjet_event
from app.integrations.chanjet.token_service import exchange_authorization_code


LOGGER = logging.getLogger(__name__)
CHANJET_SUCCESS_RESPONSE = {"result": "success"}


def handle_chanjet_webhook(
    payload: dict[str, Any],
    event_sink: Callable[[ChanjetEvent, dict[str, Any]], None] | None = None,
) -> dict[str, str]:
    try:
        event = decode_chanjet_payload(payload)
        record = _event_to_record(event)
        spool_chanjet_record("event", record)
        if event_sink is not None:
            event_sink(event, record)
        LOGGER.info(
            "chanjet webhook received msg_type=%s event_id=%s",
            event.msg_type,
            event.event_id,
        )
    except Exception as exc:
        spool_chanjet_record("error", {"error": str(exc), "payload_keys": sorted(payload.keys())})
        LOGGER.warning("chanjet webhook accepted but not processed: %s", exc)
    return CHANJET_SUCCESS_RESPONSE


def handle_chanjet_oauth_callback(code: str | None, state: str | None, redirect_uri: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "code_received": bool(code),
        "state": state,
        "redirect_uri": redirect_uri,
    }
    if code and redirect_uri and _truthy(os.getenv("CHANJET_AUTO_EXCHANGE_OAUTH_CODE")):
        try:
            token_response = exchange_authorization_code(code, redirect_uri)
            record["token_response"] = token_response
            record["token_summary"] = summarize_token_response(token_response)
        except Exception as exc:
            record["exchange_error"] = str(exc)
            LOGGER.warning("chanjet oauth code accepted but token exchange failed: %s", exc)

    spool_chanjet_record("oauth", record)
    return {"result": "success", "code_received": bool(code)}


def decode_chanjet_payload(payload: dict[str, Any]) -> ChanjetEvent:
    if "encryptMsg" not in payload:
        return parse_chanjet_event(payload)

    aes_key = _chanjet_webhook_aes_key()
    if not aes_key:
        raise RuntimeError("CHANJET_WEBHOOK_AES_KEY is required to decrypt Chanjet webhook payload")

    decrypted = decrypt_encrypt_msg(str(payload["encryptMsg"]), aes_key)
    decoded = json.loads(decrypted)
    if not isinstance(decoded, dict):
        raise ValueError("decrypted Chanjet payload must be a JSON object")
    return parse_chanjet_event(decoded)


def spool_chanjet_record(record_type: str, record: dict[str, Any]) -> None:
    spool_dir = os.getenv("CHANJET_EVENT_SPOOL_DIR", "/tmp/aliecs-integration-events/chanjet").strip()
    if not spool_dir:
        return

    os.makedirs(spool_dir, exist_ok=True)
    file_name = f"{int(time.time() * 1000)}-{record_type}.json"
    path = os.path.join(spool_dir, file_name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)


def summarize_token_response(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    if not isinstance(result, dict):
        result = response.get("value")
    if not isinstance(result, dict):
        return {"has_token": False}

    token = result.get("access_token") or result.get("accessToken") or result.get("orgAccessToken")
    refresh_token = result.get("refresh_token") or result.get("refreshToken")
    return {
        "has_token": bool(token),
        "token_len": len(str(token)) if token else 0,
        "has_refresh_token": bool(refresh_token),
        "refresh_token_len": len(str(refresh_token)) if refresh_token else 0,
        "expires_in": result.get("expires_in") or result.get("expiresIn") or result.get("expireTime"),
        "org_id": result.get("org_id") or result.get("orgId"),
        "app_name": result.get("app_name") or result.get("appName"),
    }


def _event_to_record(event: ChanjetEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "msg_type": event.msg_type,
        "app_key": event.app_key,
        "app_id": event.app_id,
        "received_time": event.received_time,
        "biz_content": event.biz_content,
        "raw": event.raw,
    }


def _chanjet_webhook_aes_key() -> str:
    value = os.getenv("CHANJET_WEBHOOK_AES_KEY", "").strip()
    if value:
        return value

    file_path = os.getenv("CHANJET_WEBHOOK_AES_KEY_FILE", "").strip()
    candidates = [file_path] if file_path else []
    candidates.extend(["/run/secrets/chanjet_webhook_aes_key", "/tmp/chanjet_webhook_aes_key"])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            continue
    return ""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}
