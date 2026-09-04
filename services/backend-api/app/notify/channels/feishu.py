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
import urllib.error
import urllib.request
import uuid
from typing import Any

from app.notify.models import Notification

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
    try:
        with opener(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 卡片结构被拒时飞书返回 HTTP 400，响应体里的 ErrPath 会精确指到是哪个元素的
        # 哪个字段（例：`ROOT -> body -> elements -> [4](tag: img); ErrMsg: img size is
        # not allowed`）。urlopen 在读响应体之前就抛 HTTPError，不在这里读出来这行就
        # 整个丢了——而 send 收到异常后会静默降级成纯文本，线上只剩「卡片怎么变成
        # 文字了」这一个现象，无从查起。2026-09-04 定位 img size 互斥就是靠这行。
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"feishu http {exc.code} on {path}: {detail}") from None
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


LEVEL_TEMPLATES = {"info": "blue", "warn": "yellow", "error": "red", "fatal": "red"}

# 页脚和「图片发送失败」这类小字。JSON 2.0 没有 note 组件（1.0 有），替代写法是
# markdown + notation 字号 + <font color> ——markdown 组件本身没有 text_color 字段。
_NOTATION = "notation"

IMAGE_FAILED_MARK = "🖼️ 图片发送失败"


def _notation(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": f"<font color='grey'>{content}</font>", "text_size": _NOTATION}


def _fields_columns(fields: list[Any]) -> list[dict[str, Any]]:
    """键值对两列排。

    JSON 1.0 的 `div.fields` + `is_short` 在 2.0 里没有对应物，等价效果要自己用
    column_set 搭：每行两列、等宽（flex_mode=bisect）。奇数个时最后一行只有一列。
    """
    rows: list[dict[str, Any]] = []
    for start in range(0, len(fields), 2):
        rows.append(
            {
                "tag": "column_set",
                "flex_mode": "bisect",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [{"tag": "markdown", "content": f"**{field.name}**\n{field.value}"}],
                    }
                    for field in fields[start : start + 2]
                ],
            }
        )
    return rows


def _button(text: str, url: str, style: str, *, width: str) -> dict[str, Any]:
    """2.0 的按钮是 body 里的一等组件，不再包在 `action` 容器里（2.0 没有 action）。

    跳转动作走 behaviors 而不是 1.0 的顶层 `url` 字段。
    """
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": style,
        "width": width,
        "behaviors": [{"type": "open_url", "default_url": url}],
    }


def build_card(notification: Notification, image_keys: dict[str, str]) -> dict[str, Any]:
    """把段落渲染成一张交互卡片，文字与图按原顺序交错——一条消息而不是几个气泡。

    2026-09-04 从卡片 JSON 1.0 升到 2.0。⚠️ 1.0 那版的三处写法在 2.0 里不存在，
    照抄会让整张卡片被拒（然后 send 静默降级成纯文本，事后看不出来）：
    `note` 组件、`action` 按钮容器、`div.fields` 两列布局。对应替代见上面三个辅助函数。
    """
    elements: list[dict[str, Any]] = []
    if notification.summary.strip():
        elements.append({"tag": "markdown", "content": _lark_md(notification.summary.strip())})
    for segment in notification.segments:
        if segment.kind == "text":
            # preformatted 段走 plain_text 组件：排版里的 *、|、# 交给 markdown 会被吃掉。
            if segment.preformatted:
                elements.append(
                    {"tag": "div", "text": {"tag": "plain_text", "content": segment.text.strip()}}
                )
            else:
                elements.append({"tag": "markdown", "content": _lark_md(segment.text.strip())})
        elif segment.kind == "fields":
            elements.extend(_fields_columns(segment.fields))
        elif segment.kind == "image":
            image_key = image_keys.get(segment.image_ref)
            if not image_key:
                # 这张图没传上去：留一行说明，别让卡片里凭空少一块。
                elements.append(_notation(IMAGE_FAILED_MARK))
                continue
            caption = next(
                (image.caption for image in notification.images if image.ref == segment.image_ref), ""
            )
            # ⚠️ 不要加 `size`。2026-09-04 对生产飞书实测：`scale_type: fit_horizontal`
            # 与 `size` 互斥，带任何 size 值（含文档列出的 stretch/large/…）整张卡片
            # 都会被拒：`ErrCode 10002 … (tag: img); ErrMsg: img size is not allowed`。
            # 不带 size 才过；crop_center / crop_top 则允许带 size。文档只列了 size 的
            # 枚举，没写这条互斥关系。
            element: dict[str, Any] = {
                "tag": "img",
                "img_key": image_key,
                "alt": {"tag": "plain_text", "content": caption or "图片"},
                "scale_type": "fit_horizontal",
                "preview": True,
            }
            if caption:
                element["title"] = {"tag": "plain_text", "content": caption}
            elements.append(element)

    buttons = notification.all_buttons()
    if len(buttons) == 1:
        elements.append(_button(buttons[0].text, buttons[0].url, buttons[0].style, width="fill"))
    elif buttons:
        # 多个按钮横排。2.0 没有 action 容器，横排要靠 column_set；flow 让窄屏自动换行。
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "flow",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [_button(button.text, button.url, button.style, width="default")],
                    }
                    for button in buttons
                ],
            }
        )

    elements.append(_notation(f"{notification.source} · {notification.event}"))

    header: dict[str, Any] = {
        "title": {"tag": "plain_text", "content": notification.display_title()},
        "template": notification.theme or LEVEL_TEMPLATES.get(notification.level, "blue"),
    }
    if notification.subtitle.strip():
        header["subtitle"] = {"tag": "plain_text", "content": notification.subtitle.strip()}
    if notification.tags:
        header["text_tag_list"] = [
            {
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": tag.text},
                "color": tag.color,
            }
            for tag in notification.tags
        ]
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "update_multi": True},
        "header": header,
        "body": {"direction": "vertical", "elements": elements},
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
