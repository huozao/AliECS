"""飞书投递。

上传与卡片结构移植自 deploy/openclaw-bridge/openclaw_bridge.py（feishu_upload_image /
build_feishu_card / _lark_md）——那套在飞书链路上已经跑了几个月，富文本、图片、
文件都验证过，没有理由重写一遍 im/v1/messages。

与 bridge 的差别只有两点：这里发的是主动通知而不是回复（没有 message_id 可 reply），
以及凭据按 profile 取而不是取单一全局 app。
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.request
import uuid
from typing import Any

from app.notify.models import LEVEL_ICONS, Notification

DEFAULT_API_BASE = "https://open.feishu.cn/open-apis"

_token_lock = threading.Lock()
# {(app_id, api_base): (token, expires_at_monotonic)}
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


def api_base(profile: str) -> str:
    return (
        os.getenv(f"FEISHU_{profile}_API_BASE")
        or os.getenv("FEISHU_API_BASE")
        or DEFAULT_API_BASE
    ).rstrip("/")


def credentials(profile: str) -> tuple[str, str]:
    """按 profile 取 app 凭据，逐级回退。

    最后一级回退到 VERSION_DIGEST_FEISHU_* 是为了收敛期的连续性：那对凭据是
    backend-api 容器里现存唯一的飞书 app，版本周报和黄金告警都在用它。
    """
    app_id = (
        os.getenv(f"FEISHU_{profile}_APP_ID")
        or os.getenv("FEISHU_APP_ID")
        or os.getenv("VERSION_DIGEST_FEISHU_APP_ID")
        or ""
    ).strip()
    app_secret = (
        os.getenv(f"FEISHU_{profile}_APP_SECRET")
        or os.getenv("FEISHU_APP_SECRET")
        or os.getenv("VERSION_DIGEST_FEISHU_APP_SECRET")
        or ""
    ).strip()
    return app_id, app_secret


def tenant_access_token(profile: str, *, opener=urllib.request.urlopen) -> str:
    app_id, app_secret = credentials(profile)
    if not (app_id and app_secret):
        raise RuntimeError(f"feishu profile {profile} has no credentials")
    base = api_base(profile)
    cache_key = (app_id, base)
    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
    request = urllib.request.Request(
        base + "/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    token = str(result.get("tenant_access_token") or "")
    if not token:
        raise RuntimeError(f"feishu tenant_access_token failed: code={result.get('code')}")
    try:
        # 与 bridge 同一口径：提前 120 秒过期，避免拿着将死的 token 去发消息。
        ttl = max(60, int(result.get("expire")) - 120)
    except (TypeError, ValueError):
        ttl = 6000
    with _token_lock:
        _token_cache[cache_key] = (token, time.monotonic() + ttl)
    return token


def _post_json(
    profile: str, path: str, payload: dict[str, Any], token: str, *, opener=urllib.request.urlopen
) -> dict[str, Any]:
    request = urllib.request.Request(
        api_base(profile) + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    with opener(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    code = result.get("code") if isinstance(result, dict) else None
    if code not in (None, 0):
        raise RuntimeError(f"feishu api error {code}: {str(result)[:200]}")
    return result


def upload_image(profile: str, data: bytes, token: str, *, opener=urllib.request.urlopen) -> str:
    """POST im/v1/images，返回 image_key。multipart 手写，与 bridge 一致（不引 requests）。"""
    boundary = "----aliecsNotify" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts = [
        b"--" + boundary.encode(),
        b'Content-Disposition: form-data; name="image_type"',
        b"",
        b"message",
        b"--" + boundary.encode(),
        b'Content-Disposition: form-data; name="image"; filename="image.png"',
        b"Content-Type: application/octet-stream",
        b"",
    ]
    body = crlf.join(parts) + crlf + data + crlf + b"--" + boundary.encode() + b"--" + crlf
    request = urllib.request.Request(
        api_base(profile) + "/im/v1/images",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        },
        method="POST",
    )
    with opener(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") not in (None, 0):
        raise RuntimeError(f"feishu image upload error: {str(result)[:200]}")
    image_key = (result.get("data") or {}).get("image_key")
    if not image_key:
        raise RuntimeError("feishu image upload returned no image_key")
    return str(image_key)


def _lark_md(text: str) -> str:
    """lark_md 不渲染 ATX 标题（## X 会原样显示），转成它认的粗体。抄自 bridge。"""
    return re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$", r"**\1**", text)


def build_card(notification: Notification, image_keys: dict[str, str]) -> dict[str, Any]:
    """把段落渲染成一张交互卡片，文字与图按原顺序交错——一条消息而不是几个气泡。"""
    icon = LEVEL_ICONS.get(notification.level, "")
    elements: list[dict[str, Any]] = []
    if notification.summary.strip():
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": _lark_md(notification.summary.strip())}}
        )
    for segment in notification.segments:
        if segment.kind == "text":
            # preformatted 段走 plain_text：排版里的 *、|、# 交给 markdown 会被吃掉。
            tag = "plain_text" if segment.preformatted else "lark_md"
            content = segment.text.strip() if segment.preformatted else _lark_md(segment.text.strip())
            elements.append({"tag": "div", "text": {"tag": tag, "content": content}})
        elif segment.kind == "fields":
            elements.append(
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**{field.name}**\n{field.value}"},
                        }
                        for field in segment.fields
                    ],
                }
            )
        elif segment.kind == "image":
            image_key = image_keys.get(segment.image_ref)
            if not image_key:
                # 这张图没传上去：留一行说明，别让卡片里凭空少一块。
                elements.append(
                    {"tag": "note", "elements": [{"tag": "plain_text", "content": "🖼️ 图片发送失败"}]}
                )
                continue
            elements.append(
                {
                    "tag": "img",
                    "img_key": image_key,
                    "alt": {"tag": "plain_text", "content": "图片"},
                    "mode": "fit_horizontal",
                    "preview": True,
                }
            )
    if notification.link is not None:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": notification.link.text},
                        "url": notification.link.url,
                        "type": "default",
                    }
                ],
            }
        )
    footer = f"{notification.source} · {notification.event}"
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{icon} {notification.title}".strip()},
            "template": {"info": "blue", "warn": "yellow", "error": "red", "fatal": "red"}.get(
                notification.level, "blue"
            ),
        },
        "elements": elements,
    }


def send(notification: Notification, target: dict[str, Any], *, opener=urllib.request.urlopen) -> None:
    """投递一条通知。失败抛异常，由 dispatch 记账重试。

    target: {"profile": "COMPANY_A", "receive_id": "oc_xxx", "receive_id_type": "chat_id"}
    """
    profile = str(target.get("profile") or "COMPANY_A").strip() or "COMPANY_A"
    receive_id = str(target.get("receive_id") or "").strip()
    receive_id_type = str(target.get("receive_id_type") or "chat_id").strip() or "chat_id"
    if not receive_id:
        raise RuntimeError("feishu target has no receive_id")

    token = tenant_access_token(profile, opener=opener)

    image_keys: dict[str, str] = {}
    for image in notification.images:
        try:
            image_keys[image.ref] = upload_image(
                profile, base64.b64decode(image.png_base64, validate=True), token, opener=opener
            )
        except Exception:
            # 单张图传不上去不该让整条消息发不出去——卡片里那一格会写「图片发送失败」。
            continue

    path = f"/im/v1/messages?receive_id_type={receive_id_type}"
    try:
        card = build_card(notification, image_keys)
        _post_json(
            profile,
            path,
            {
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            token,
            opener=opener,
        )
        return
    except Exception:
        # 卡片被拒（结构、字段长度、权限都可能）时退回纯文本：图丢了但字还在。
        # 与 gold_spread_alerts 现有的降级策略同口径。
        pass

    _post_json(
        profile,
        path,
        {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": notification.plain_text()}, ensure_ascii=False),
        },
        token,
        opener=opener,
    )
