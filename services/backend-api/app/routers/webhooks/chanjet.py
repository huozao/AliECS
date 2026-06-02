from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query

from app.integrations.chanjet.handlers import handle_chanjet_oauth_callback, handle_chanjet_webhook


router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post("/chanjet")
def receive_chanjet_webhook(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return handle_chanjet_webhook(payload)


@router.get("/chanjet/oauth")
def receive_chanjet_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    redirect_uri: str | None = Query(default=None, alias="redirectUri"),
) -> dict[str, Any]:
    return handle_chanjet_oauth_callback(code=code, state=state, redirect_uri=redirect_uri)
