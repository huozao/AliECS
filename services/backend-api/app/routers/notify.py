"""统一消息中枢的 HTTP 入口。

只有两个面向机器的端点：外部生产者投消息（/send），以及带走积压重试（/flush）。
服务端内部的生产者不走这里——它们直接 import app.notify.dispatch.deliver。
"""

from __future__ import annotations

import hmac
import os
from contextlib import closing
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from app.core import _conn
from app.notify import dispatch, store
from app.notify.models import Notification

router = APIRouter()


def _require_source(
    x_notify_source: str | None = Header(default=None),
    x_notify_token: str | None = Header(default=None),
) -> str:
    """按来源校验 token。每个来源一个 token，可单独吊销。"""
    source_key = (x_notify_source or "").strip()
    token = (x_notify_token or "").strip()
    if not (source_key and token):
        raise HTTPException(status_code=401, detail="missing notify credentials")
    with closing(_conn()) as conn:
        if not store.verify_source_token(conn, source_key, token):
            raise HTTPException(status_code=401, detail="invalid notify credentials")
    return source_key


def _require_flush_token(x_notify_flush_token: str | None = Header(default=None)) -> None:
    """flush 是内部端点（worker 主循环每轮调一次），单独一个 token。"""
    expected = os.getenv("NOTIFY_FLUSH_TOKEN", "").strip()
    supplied = (x_notify_flush_token or "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="invalid flush token")


@router.post("/v1/internal/notify/send")
def send_notification(
    body: Notification,
    source_key: str = Depends(_require_source),
) -> dict[str, Any]:
    """投递一条通知。

    ``source`` 必须与请求头里的来源一致——否则一个来源的 token 就能冒充别的来源，
    绕开按来源配置的路由。
    """
    if body.source != source_key:
        raise HTTPException(status_code=403, detail="source mismatch")
    result = dispatch.deliver(body)
    if result["duplicate"]:
        if result["sent"] > 0:
            return {"ok": True, "delivered": True, **result}
        reason = (
            "no matching route"
            if result["targets"] == 0
            else "queued for retry"
            if result.get("pending", 0) > 0
            else "delivery exhausted"
        )
        return {"ok": True, "delivered": False, "reason": reason, **result}
    if result["targets"] == 0:
        # 落库了但没有任何路由命中。显式返回 delivered=false：调用方需要知道
        # 「收下了」和「发出去了」不是一回事，否则配置缺失会被当成投递成功。
        return {"ok": True, "delivered": False, "reason": "no matching route", **result}
    if result["sent"] == 0:
        raise HTTPException(
            status_code=502,
            detail=f"all {result['targets']} targets failed; queued for retry (outbox {result['outbox_id']})",
        )
    return {"ok": True, "delivered": True, **result}


@router.get("/v1/internal/notify/deliveries/{outbox_id}")
def get_delivery_receipt(
    outbox_id: int,
    source_key: str = Depends(_require_source),
) -> dict[str, Any]:
    """返回一条消息的实际投递凭证，并按已鉴权来源隔离。"""
    with closing(_conn()) as conn:
        receipt = store.delivery_receipt(conn, outbox_id, source_key)
    if receipt is None:
        # 不区分“不存在”和“属于其他来源”，避免泄漏其他生产者的消息编号。
        raise HTTPException(status_code=404, detail="notification delivery not found")
    return {"ok": True, **receipt}


@router.post("/v1/internal/notify/flush")
def flush_pending(
    limit: int = 50,
    _: None = Depends(_require_flush_token),
) -> dict[str, Any]:
    return {"ok": True, **dispatch.flush(limit=max(1, min(limit, 200)))}
