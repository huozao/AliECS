from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Query

from app.integrations.chanjet.handlers import handle_chanjet_oauth_callback, handle_chanjet_webhook
from app.integrations.chanjet.schemas import ChanjetEvent
from app.integrations.store import save_chanjet_event_with_configured_database


router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
LOGGER = logging.getLogger(__name__)


@router.post("/chanjet")
def receive_chanjet_webhook(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return handle_chanjet_webhook(payload, event_sink=_save_chanjet_event)


@router.get("/chanjet/oauth")
def receive_chanjet_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    redirect_uri: str | None = Query(default=None, alias="redirectUri"),
) -> dict[str, Any]:
    return handle_chanjet_oauth_callback(code=code, state=state, redirect_uri=redirect_uri)


def _save_chanjet_event(event: ChanjetEvent, record: dict[str, Any]) -> None:
    try:
        save_chanjet_event_with_configured_database(event, record)
    except Exception as exc:
        LOGGER.warning("chanjet webhook event accepted but database store failed: %s", exc)
