"""企微投递，两条路：群机器人 webhook（wecom_bot）与自建应用消息（wecom_app）。

两者的能力边界不同，渲染也就不同：
- 群机器人：只能发到固定群，不需要应用凭据；markdown 上限 4096 字节；图要单独一条
  消息（base64 + md5），不能和 markdown 拼在一条里。
- 自建应用：可发给指定人/部门/标签；图要先 media/upload 换 media_id。

所以「一条通知」在企微侧可能落成多条消息（正文一条 + 每张图一条）。飞书那边是一张
卡片装下全部——这是各家 IM 的能力差异，不该反过来削平消息模型。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

from app.notify.models import Notification

API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"

# 企微 markdown 正文上限 4096 字节（UTF-8），留一点余量给截断提示。
MARKDOWN_MAX_BYTES = 4000
# 群机器人图片：base64 后 2MB 上限，且必须是 jpg/png。
BOT_IMAGE_MAX_BYTES = 2 * 1024 * 1024

_token_lock = threading.Lock()
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}

LEVEL_COLORS = {"info": "info", "warn": "warning", "error": "warning", "fatal": "warning"}


TRUNCATION_SUFFIX = "\n…（已截断）"


def _truncate_utf8(text: str, limit: int = MARKDOWN_MAX_BYTES) -> str:
    """按 UTF-8 字节数截断。

    预留量必须按后缀的**实际字节数**算，不能拍一个常数——后缀是中文，
    七个字符就是 19 字节，写死 12 会让结果反而超限。
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    reserved = len(TRUNCATION_SUFFIX.encode("utf-8"))
    return encoded[: max(0, limit - reserved)].decode("utf-8", "ignore") + TRUNCATION_SUFFIX


def render_markdown(notification: Notification) -> str:
    """把通知渲染成企微 markdown。

    企微 markdown 不支持表格和图片，所以 fields 走「**名**：值」的列表行，
    image 段在这里只留标题占位，真正的图另发一条消息。
    """
    color = LEVEL_COLORS.get(notification.level, "info")
    lines = [f'<font color="{color}">**{notification.display_title()}**</font>']
    # 企微没有标签组件，也没有副标题——降级成标题下的两行，信息不丢就行。
    if notification.tags:
        lines.append(" ".join(f"`{tag.text}`" for tag in notification.tags))
    if notification.subtitle.strip():
        lines.append(notification.subtitle.strip())
    if notification.summary.strip():
        lines.append(notification.summary.strip())
    for segment in notification.segments:
        if segment.kind == "text":
            lines.append(segment.text.strip())
        elif segment.kind in {"fields", "section"}:
            if segment.kind == "section":
                heading = " ".join(
                    part for part in (segment.section_icon.strip(), segment.section_title.strip()) if part
                )
                if heading:
                    lines.append(f"**{heading}**")
                if segment.section_subtitle.strip():
                    lines.append(segment.section_subtitle.strip())
            lines.extend(f"**{field.name}**：{field.value}" for field in segment.fields)
    # 企微 markdown 没有按钮，一律降级成链接行——每个按钮一行，样式（primary/danger）丢掉。
    for button in notification.all_buttons():
        lines.append(f"[{button.text}]({button.url})")
    lines.append(f"> {notification.source} · {notification.event}")
    return _truncate_utf8("\n".join(line for line in lines if line).strip())


def _post(url: str, payload: dict[str, Any], *, opener=urllib.request.urlopen, timeout: int = 20) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with opener(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errcode") not in (None, 0):
        raise RuntimeError(f"wecom api error {result.get('errcode')}: {str(result.get('errmsg'))[:160]}")
    return result


# --------------------------------------------------------------------------- 群机器人


def send_bot(notification: Notification, target: dict[str, Any], *, opener=urllib.request.urlopen) -> None:
    """target: {"webhook_env": "GROUP1_WEBHOOK"}

    DB 里只存 env 变量名，webhook URL 本身留在 SOPS 渲染出来的环境变量里——
    这个 URL 等同于凭据，任何拿到它的人都能往群里发消息。
    """
    env_name = str(target.get("webhook_env") or "").strip()
    if not env_name:
        raise RuntimeError("wecom_bot target has no webhook_env")
    webhook = (os.getenv(env_name) or "").strip()
    if not webhook:
        raise RuntimeError(f"wecom_bot webhook env {env_name} is empty")

    _post(webhook, {"msgtype": "markdown", "markdown": {"content": render_markdown(notification)}}, opener=opener)

    for image in notification.images:
        try:
            data = base64.b64decode(image.png_base64, validate=True)
        except Exception:
            continue
        if len(data) > BOT_IMAGE_MAX_BYTES:
            # 超限就跳过这张图，正文已经发出去了，不该因为一张图把整条判成失败。
            continue
        _post(
            webhook,
            {
                "msgtype": "image",
                "image": {
                    "base64": base64.b64encode(data).decode(),
                    "md5": hashlib.md5(data).hexdigest(),
                },
            },
            opener=opener,
        )


# --------------------------------------------------------------------------- 自建应用


def app_credentials(profile: str) -> tuple[str, str, str]:
    """返回 (corp_id, app_secret, agent_id)。

    ⚠️ 历史 SOPS 键名是 ``WECOM_COMPANY_A_gentId``（少了个 A，历史 typo），这里两种
    拼写都认。**但 2026-08-31 实测发现那个键的值本身是错的**：
    ``WECOM_COMPANY_A_gentId=1000003``，而 1000003 是企微 **B** 的 agentid，A 的正确值
    是 1000005（各自 ``APP_SECRET`` 调 ``agent/get`` 验证：A 的 token 操作 1000003 被拒
    ``301002 not allow operate another agent with this accesstoken``）。

    所以顺序不能反：``*_AGENT_ID`` 是规范键、优先；``*_gentId`` 只是兼容回落。
    上线到 2026-08-31 之间 wecom_app 一次都没被真实调用过，所以这个错配没暴露——
    只要有任何路由指向 wecom_app + COMPANY_A，投递就会一路失败到 dead。
    """
    corp_id = (os.getenv(f"WECOM_{profile}_CORP_ID") or "").strip()
    app_secret = (os.getenv(f"WECOM_{profile}_APP_SECRET") or "").strip()
    agent_id = (
        os.getenv(f"WECOM_{profile}_AGENT_ID")
        or os.getenv(f"WECOM_{profile}_gentId")
        or ""
    ).strip()
    return corp_id, app_secret, agent_id


def access_token(profile: str, *, opener=urllib.request.urlopen) -> str:
    corp_id, app_secret, _ = app_credentials(profile)
    if not (corp_id and app_secret):
        raise RuntimeError(f"wecom profile {profile} has no credentials")
    cache_key = (corp_id, app_secret[:8])
    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
    url = f"{API_BASE}/gettoken?corpid={urllib.parse.quote(corp_id)}&corpsecret={urllib.parse.quote(app_secret)}"
    request = urllib.request.Request(url, method="GET")
    with opener(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errcode") not in (None, 0):
        raise RuntimeError(f"wecom gettoken failed: {result.get('errcode')}")
    token = str(result.get("access_token") or "")
    if not token:
        raise RuntimeError("wecom gettoken returned no access_token")
    try:
        ttl = max(60, int(result.get("expires_in")) - 120)
    except (TypeError, ValueError):
        ttl = 6000
    with _token_lock:
        _token_cache[cache_key] = (token, time.monotonic() + ttl)
    return token


def upload_image_media(token: str, data: bytes, *, opener=urllib.request.urlopen) -> str:
    """media/upload type=image，返回 media_id（三天有效，发完即用不需要缓存）。"""
    boundary = "----aliecsNotify" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts = [
        b"--" + boundary.encode(),
        b'Content-Disposition: form-data; name="media"; filename="image.png"',
        b"Content-Type: image/png",
        b"",
    ]
    body = crlf.join(parts) + crlf + data + crlf + b"--" + boundary.encode() + b"--" + crlf
    request = urllib.request.Request(
        f"{API_BASE}/media/upload?access_token={urllib.parse.quote(token)}&type=image",
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST",
    )
    with opener(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errcode") not in (None, 0):
        raise RuntimeError(f"wecom media upload error: {result.get('errcode')}")
    media_id = str(result.get("media_id") or "")
    if not media_id:
        raise RuntimeError("wecom media upload returned no media_id")
    return media_id


def send_app(notification: Notification, target: dict[str, Any], *, opener=urllib.request.urlopen) -> None:
    """target: {"profile": "COMPANY_A", "touser": "@all", "toparty": "", "totag": ""}"""
    profile = str(target.get("profile") or "COMPANY_A").strip() or "COMPANY_A"
    corp_id, app_secret, agent_id = app_credentials(profile)
    if not agent_id:
        raise RuntimeError(f"wecom profile {profile} has no agent id")
    token = access_token(profile, opener=opener)
    url = f"{API_BASE}/message/send?access_token={urllib.parse.quote(token)}"

    recipients = {
        key: str(target.get(key) or "")
        for key in ("touser", "toparty", "totag")
        if str(target.get(key) or "").strip()
    }
    if not recipients:
        recipients = {"touser": "@all"}

    payload: dict[str, Any] = {
        **recipients,
        "msgtype": "markdown",
        "agentid": int(agent_id),
        "markdown": {"content": render_markdown(notification)},
    }
    _post(url, payload, opener=opener)

    for image in notification.images:
        try:
            data = base64.b64decode(image.png_base64, validate=True)
            media_id = upload_image_media(token, data, opener=opener)
        except Exception:
            continue
        _post(
            url,
            {**recipients, "msgtype": "image", "agentid": int(agent_id), "image": {"media_id": media_id}},
            opener=opener,
        )
