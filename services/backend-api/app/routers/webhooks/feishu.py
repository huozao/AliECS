from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.integrations.feishu.handlers import handle_feishu_webhook


LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post("/feishu")
async def receive_feishu_webhook(request: Request) -> dict[str, Any]:
    body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        return handle_feishu_webhook(body, headers)
    except PermissionError as exc:
        LOGGER.warning("feishu webhook rejected: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        LOGGER.warning("feishu webhook bad payload: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
