"""投递通道。每个通道暴露 send(notification, target) —— 成功返回，失败抛异常。

异常由 dispatch 记账并安排重试；通道内部只处理「能降级就降级」的局部失败
（例如某一张图传不上去），不吞掉整条消息的失败。
"""

from __future__ import annotations

from typing import Any, Callable

from app.notify.channels import feishu, wecom
from app.notify.models import Notification

Sender = Callable[[Notification, dict[str, Any]], None]

SENDERS: dict[str, Sender] = {
    "feishu": feishu.send,
    "wecom_bot": wecom.send_bot,
    "wecom_app": wecom.send_app,
}


def sender_for(channel: str) -> Sender:
    try:
        return SENDERS[channel]
    except KeyError:
        raise RuntimeError(f"unknown notify channel: {channel}") from None
