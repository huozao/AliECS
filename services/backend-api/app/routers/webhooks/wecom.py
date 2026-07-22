from __future__ import annotations

import logging
import os
import threading
import time

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.integrations.wecom_kf import (
    WeComKfConfig,
    WeComKfError,
    crypto_for_config,
    handle_notification,
    parse_kf_notification,
    poll_accounts_once,
)


router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

logger = logging.getLogger("aliecs.wecom_kf")


def _load_kf_config() -> WeComKfConfig:
    try:
        return WeComKfConfig.from_env()
    except WeComKfError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/wecom")
def receive_wecom_webhook(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return {
        "status": "received",
        "provider": "wecom",
        "mode": "placeholder",
        "received_keys": sorted(payload.keys()),
    }


@router.get("/wecom-kf", response_class=PlainTextResponse)
def verify_wecom_kf_url(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> PlainTextResponse:
    try:
        config = _load_kf_config()
        plain = crypto_for_config(config).decrypt_url_echo(
            msg_signature, timestamp, nonce, echostr
        )
        return PlainTextResponse(plain)
    except WeComKfError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/wecom-kf", response_class=PlainTextResponse)
async def receive_wecom_kf_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
) -> PlainTextResponse:
    try:
        config = _load_kf_config()
        plain_xml = crypto_for_config(config).decrypt_callback(
            msg_signature, timestamp, nonce, await request.body()
        )
        notification = parse_kf_notification(plain_xml)
    except WeComKfError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if notification is not None:
        logger.info("wecom kf callback received: kfid=%s", notification.open_kfid)
        background_tasks.add_task(handle_notification, notification, config)
    return PlainTextResponse("success")


def _kf_poll_loop(interval: float) -> None:
    while True:
        time.sleep(interval)
        try:
            config = WeComKfConfig.from_env()
        except WeComKfError:
            continue
        try:
            polled = poll_accounts_once(config)
            logger.debug("wecom kf poll tick: accounts=%d", polled)
        except Exception:
            logger.exception("wecom kf poll failed")


@router.on_event("startup")
def _start_kf_poller() -> None:
    raw = os.getenv("WECOM_KF_POLL_INTERVAL_SECONDS", "300").strip()
    try:
        interval = float(raw)
    except ValueError:
        logger.warning("WECOM_KF_POLL_INTERVAL_SECONDS 不是数字，兜底轮询未启动")
        return
    if interval <= 0:
        return
    try:
        WeComKfConfig.from_env()
    except WeComKfError:
        return
    threading.Thread(
        target=_kf_poll_loop, args=(interval,), name="wecom-kf-poller", daemon=True
    ).start()
    logger.info("wecom kf poller started: interval=%ss", interval)
