from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body


router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post("/feishu")
def receive_feishu_webhook(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return {
        "status": "received",
        "provider": "feishu",
        "mode": "placeholder",
        "received_keys": sorted(payload.keys()),
    }
