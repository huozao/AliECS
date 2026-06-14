#!/usr/bin/env python3
from __future__ import annotations

import json
import base64
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Condition, Lock, Thread
from typing import Any


FALLBACK_MESSAGE = os.getenv("WEB_DOCK_FALLBACK_MESSAGE", "ChatGPT 浏览器暂不可用，请稍后再试。")
NO_REPLY = "__OPENCLAW_BRIDGE_NO_REPLY__"
OPENCLAW_METADATA_PREFIX_RE = re.compile(
    r"^(?:\[[^\]\n]*UTC\]\s*)?Conversation info \(untrusted metadata\):\s*",
    flags=re.DOTALL,
)
MAX_BRIDGE_IMAGES = 4
MAX_BRIDGE_IMAGE_BYTES = 20 * 1024 * 1024

# MIME types forwarded to webdock as file attachments.
# text/* is already inlined by OpenClaw as <file ...> blocks in the message text,
# so those are skipped here (no duplicate upload). application/octet-stream
# (truly unknown binary) is also skipped — better to skip than crash.
SUPPORTED_ATTACHMENT_MIMES = frozenset({
    # Images — existing upload path
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp",
    "image/heic", "image/heif", "image/avif",
    # Binary documents — new upload path (send-button-enabled wait in webdock)
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",   # .docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",         # .xlsx
    "application/vnd.openxmlformats-officedocument.presentationml.presentation", # .pptx
    "application/msword",           # .doc
    "application/vnd.ms-excel",     # .xls
    "application/vnd.ms-powerpoint",  # .ppt
    "application/zip",              # ZIP-based Office file with no detectable subtype
})
RECENT_METADATA_WINDOW_SECONDS = 120.0
# OpenClaw forwards inbound DOCUMENTS (PDF/DOCX/XLSX/PPTX…) as a <file name="…"
# mime="…">…</file> block, NOT a media:// reference. For binary documents the block
# body is only a placeholder ("[PDF content rendered to images; images not forwarded
# to model]") — the real bytes live in the inbound media dir under <name>. (text/*
# files differ: OpenClaw inlines their content in the body, so we leave those alone.)
OPENCLAW_FILE_BLOCK_RE = re.compile(
    r'<file\s+name="(?P<name>[^"]*)"\s+mime="(?P<mime>[^"]*)"\s*>(?P<body>.*?)</file>',
    flags=re.DOTALL,
)
OPENCLAW_MEDIA_URI_RE = re.compile(r"media://inbound/([^\s\]\)\"'`]+)")
OPENCLAW_MEDIA_ATTACHED_LINE_RE = re.compile(r"^\s*\[media attached:\s+media://inbound/[^\]]+\]\s*$", re.MULTILINE)
# Captures the bare media ID from an explicit attachment line so we only extract
# media that was freshly attached to THIS message, not historical references in
# OpenClaw conversation-context blocks (<conversation>…</conversation>).
# The real format carries a trailing type annotation, e.g.
#   [media attached: media://inbound/<id>.jpg (image/*)]
# so after the ID (which stops at the first space) we tolerate anything up to ].
OPENCLAW_MEDIA_ATTACHED_CAPTURE_RE = re.compile(
    r"\[media attached:\s+media://inbound/([^\]\s]+)[^\]]*\]",
    re.MULTILINE,
)
OPENCLAW_MEDIA_NO_CAPTION_RE = re.compile(r"^\s*\[User sent media without caption\]\s*$", re.MULTILINE)
MEDIA_INTENT_RE = re.compile(
    r"(图片|照片|这张图|图像|头像|原图|参考图|风格图|背景|修图|改图|抠图|"
    r"第一张|第二张|第三张|两张|多张|image|photo|picture|avatar|reference)",
    re.IGNORECASE,
)
CN_IMAGE_COUNT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4}
OPENCLAW_MEDIA_HELPER_PREFIXES = (
    "To send an image back, prefer ",
    "If you must inline, use MEDIA:",
    "Absolute and ~ paths only work ",
    "read boundary; host file:// URLs are blocked.",
    "body.",
)
ENGLISH_ERROR_PATTERNS = (
    "llm request timed out",
    "model idle timeout",
    "the model did not produce a response",
    "request timed out",
    "fetch failed",
    "chatgpt response did not finish before timeout",
    "cannot find chatgpt input box",
    "chatgpt is not logged in",
)
_recent_lane_metadata: dict[str, Any] = {}
_recent_lane_metadata_at = 0.0
_pending_batches: dict[str, Any] = {}
_pending_batches_lock = Lock()


class PendingBatch:
    def __init__(self, body: dict[str, Any], details: dict[str, Any], wait_seconds: float) -> None:
        self.condition = Condition()
        self.body = body
        self.user_text = details["user_text"]
        self.images = list(details["images"])
        self.metadata = dict(details["metadata"])
        self.expected_images = expected_image_count(details)
        self.created = time.monotonic()
        self.deadline = self.created + wait_seconds
        self.updated = self.created

    def merge(self, details: dict[str, Any]) -> None:
        if details["user_text"]:
            self.user_text = details["user_text"]
            self.expected_images = max(self.expected_images, expected_image_count(details))
        self.images.extend(details["images"])
        self.images = self.images[:MAX_BRIDGE_IMAGES]
        for key, value in details["metadata"].items():
            if value and not self.metadata.get(key):
                self.metadata[key] = value
        self.updated = time.monotonic()

    def has_text_and_images(self) -> bool:
        return bool(self.user_text and self.images)

    def has_expected_images(self) -> bool:
        return self.has_text_and_images() and len(self.images) >= max(1, self.expected_images)

    def to_body(self) -> dict[str, Any]:
        body = dict(self.body)
        body["messages"] = [{"role": "user", "content": build_outbound_content(self.user_text, self.images)}]
        if self.metadata:
            body["metadata"] = dict(self.metadata)
        return body


def clean_user_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    _metadata, metadata_end = parse_openclaw_metadata_prefix(text)
    cleaned = text[metadata_end:] if metadata_end else text
    cleaned = replace_binary_file_blocks(cleaned)
    return strip_openclaw_media_helper_text(cleaned).strip()


def replace_binary_file_blocks(text: str) -> str:
    """Replace a binary-document <file>…</file> block with a short note. Its body is
    only a placeholder ("[PDF content rendered to images…]") and the file itself is
    uploaded separately, so the note tells the model a file was attached without the
    noisy placeholder. text/* blocks (real inlined content) are left untouched."""
    def _repl(match: "re.Match[str]") -> str:
        mime = (match.group("mime") or "").strip().lower()
        if mime.startswith("text/"):
            return match.group(0)
        name = (match.group("name") or "").strip()
        return f"（已上传文件：{name}）" if name else "（已上传文件）"
    return OPENCLAW_FILE_BLOCK_RE.sub(_repl, text)


def strip_openclaw_media_helper_text(text: str) -> str:
    text = OPENCLAW_MEDIA_ATTACHED_LINE_RE.sub("", text)
    text = OPENCLAW_MEDIA_NO_CAPTION_RE.sub("", text)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in OPENCLAW_MEDIA_HELPER_PREFIXES):
            continue
        lines.append(line)
    return "\n".join(lines)


def extract_openclaw_metadata(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        return {}
    metadata, _metadata_end = parse_openclaw_metadata_prefix(text)
    return metadata


def parse_openclaw_metadata_prefix(text: str) -> tuple[dict[str, Any], int]:
    match = OPENCLAW_METADATA_PREFIX_RE.match(text)
    if not match:
        return {}, 0

    pos = match.end()
    payload: Any = None
    metadata_end = 0

    fence = re.match(r"```json\s*", text[pos:], flags=re.IGNORECASE)
    if fence:
        payload_start = pos + fence.end()
        payload_end = text.find("```", payload_start)
        if payload_end < 0:
            return {}, 0
        try:
            payload = json.loads(text[payload_start:payload_end])
        except Exception:
            payload = None
        metadata_end = payload_end + 3
    else:
        language = re.match(r"json(?:\s+|$)", text[pos:], flags=re.IGNORECASE)
        if language:
            pos += language.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
        try:
            payload, metadata_end = json.JSONDecoder().raw_decode(text, pos)
        except json.JSONDecodeError:
            return {}, 0

    while metadata_end < len(text) and text[metadata_end].isspace():
        metadata_end += 1
    return (payload if isinstance(payload, dict) else {}), metadata_end


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def get_last_user_message(messages: Any) -> str:
    raw = get_last_user_raw_text(messages)
    return clean_user_text(raw)


def get_last_user_raw_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return extract_text(msg.get("content"))
    return ""


def get_last_user_metadata(messages: Any) -> dict[str, Any]:
    return extract_openclaw_metadata(get_last_user_raw_text(messages))


def extract_image_parts(content: Any) -> list[dict[str, Any]]:
    """Normalize any image parts in an OpenClaw message content to the OpenAI
    vision shape WebDock expects: {"type": "image_url", "image_url": {"url": ...}}.
    Accepts both {"image_url": {"url": ...}} and {"image_url": "<url>"}; URLs may
    be http(s) or base64 data URLs. Text-only content yields nothing."""
    if isinstance(content, str):
        return extract_openclaw_media_image_parts(content)
    if not isinstance(content, list):
        return []
    parts: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        for key in ("text", "content"):
            value = item.get(key)
            if isinstance(value, str):
                parts.extend(extract_openclaw_media_image_parts(value))
        image_url = item.get("image_url")
        if image_url is None:
            continue
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url, str) and url.strip():
            parts.append({"type": "image_url", "image_url": {"url": url.strip()}})
    return parts[:MAX_BRIDGE_IMAGES]


_CONVERSATION_BLOCK_RE = re.compile(r"<conversation>.*?</conversation>", re.DOTALL)


def extract_openclaw_media_image_parts(text: str) -> list[dict[str, Any]]:
    """Forward freshly attached inbound media to webdock as data-URL parts.

    Two sources, both scanned only OUTSIDE any <conversation> history block (so old
    images/files from a context checkpoint are not re-attached):
      1. [media attached: media://inbound/<id>] lines — inbound images.
      2. <file name="..." mime="..."> blocks — binary documents (PDF/DOCX/…). The
         block body is just a placeholder; the real file lives in the inbound media
         dir under <name>, so we read it by name and upload the actual document.
         text/* blocks are skipped (OpenClaw already inlines their content)."""
    if not text:
        return []
    # Remove any conversation-history block before scanning for fresh attachments.
    scan_text = _CONVERSATION_BLOCK_RE.sub("", text)
    parts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_id in OPENCLAW_MEDIA_ATTACHED_CAPTURE_RE.findall(scan_text):
        if raw_id in seen:
            continue
        seen.add(raw_id)
        data_url = resolve_openclaw_inbound_media(raw_id)
        if data_url:
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
        if len(parts) >= MAX_BRIDGE_IMAGES:
            break
    for match in OPENCLAW_FILE_BLOCK_RE.finditer(scan_text):
        if len(parts) >= MAX_BRIDGE_IMAGES:
            break
        mime = (match.group("mime") or "").strip().lower()
        name = (match.group("name") or "").strip()
        if not name or name in seen or mime.startswith("text/"):
            continue  # text/* is inlined by OpenClaw; skip blanks/duplicates
        seen.add(name)
        data_url = resolve_openclaw_inbound_media(name)
        if data_url:
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
    return parts[:MAX_BRIDGE_IMAGES]


def resolve_openclaw_inbound_media(raw_id: str) -> str | None:
    media_id = urllib.parse.unquote(raw_id).strip()
    if not media_id or any(ch in media_id for ch in ("/", "\\", "\x00")) or media_id in {".", ".."}:
        return None
    base_dir = os.path.abspath(os.getenv("OPENCLAW_INBOUND_MEDIA_DIR", "/root/.openclaw/media/inbound"))
    path = os.path.abspath(os.path.join(base_dir, media_id))
    if not path.startswith(base_dir + os.sep):
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BRIDGE_IMAGE_BYTES + 1)
    except OSError:
        return None
    if not data or len(data) > MAX_BRIDGE_IMAGE_BYTES:
        return None
    mime = guess_file_mime(path, data)
    if mime not in SUPPORTED_ATTACHMENT_MIMES:
        # text/* is already inlined by OpenClaw; truly unknown binaries are skipped.
        return None
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


def _sniff_zip_content_type(data: bytes) -> str:
    """Identify OOXML subtype by scanning uncompressed filenames in ZIP local headers."""
    chunk = data[:2048]
    if b"word/" in chunk:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if b"xl/" in chunk:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if b"ppt/" in chunk:
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return "application/zip"


def guess_file_mime(path: str, data: bytes) -> str:
    """Determine MIME: file extension first, then magic bytes."""
    mime = mimetypes.guess_type(path)[0]
    if mime:
        return mime
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:2] == b"PK":
        return _sniff_zip_content_type(data)
    return "application/octet-stream"


def get_last_user_images(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return extract_image_parts(msg.get("content"))
    return []


def webdock_configured() -> bool:
    return bool(os.getenv("WEB_DOCK_BASE_URL") and os.getenv("WEB_DOCK_API_TOKEN"))


def webdock_url() -> str:
    return os.getenv("WEB_DOCK_BASE_URL", "").rstrip("/") + "/chat/completions"


def webdock_media_root() -> str:
    """WebDock root (without the /v1 suffix) for proxying /media/<token>."""
    base = os.getenv("WEB_DOCK_BASE_URL", "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def webdock_timeout() -> int:
    # Must be >= WebDock's chat_timeout_seconds (prod runtime override ~300s for
    # long reasoning + image work), otherwise the bridge gives up before WebDock
    # returns the real reply. The SSE keepalive (stream_sse) covers OpenClaw's
    # ~120s idle limit during this wait.
    try:
        return max(5, int(os.getenv("WEB_DOCK_TIMEOUT_SECONDS", "320")))
    except ValueError:
        return 320


def request_details(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages")
    user_text = get_last_user_message(messages)
    images = get_last_user_images(messages)
    metadata = build_webdock_metadata(body)
    if not metadata.get("peer_id"):
        inherited_metadata = get_recent_lane_metadata()
        if inherited_metadata:
            inherited_metadata.update(metadata)
            metadata = inherited_metadata
    return {"request_id": uuid.uuid4().hex[:12], "user_text": user_text, "images": images, "metadata": metadata}


def build_webdock_body(body: dict[str, Any]) -> dict[str, Any]:
    details = request_details(body)
    outbound = {
        "model": os.getenv("WEB_DOCK_MODEL", "browser-chatgpt"),
        "messages": [{"role": "user", "content": build_outbound_content(details["user_text"], details["images"])}],
        "stream": False,
    }
    metadata = details["metadata"]
    if metadata:
        outbound["metadata"] = metadata
        remember_lane_metadata(metadata)
    return outbound


def build_outbound_content(user_text: str, images: list[dict[str, Any]]) -> Any:
    """Plain string when there are no images (unchanged behavior); otherwise the
    OpenAI vision parts list so WebDock uploads the image(s). An image with no
    caption is forwarded with no text part (WeChat sends text and each image as
    separate messages)."""
    if not images:
        return user_text or "请回复这条微信消息。"
    parts: list[dict[str, Any]] = []
    if user_text:
        parts.append({"type": "text", "text": user_text})
    parts.extend(images)
    return parts


def build_webdock_metadata(body: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if isinstance(body.get("metadata"), dict):
        metadata.update(body["metadata"])
    metadata.update(get_last_user_metadata(body.get("messages")))

    normalized: dict[str, Any] = {}
    channel = normalize_channel(_first_metadata_value(metadata, "channel", "platform", "source", "adapter"))
    if not channel and _looks_like_feishu(metadata):
        channel = "feishu"
    channel = channel or "wechat"
    wechat_account = _first_metadata_value(metadata, "wechat_account", "account", "channel_id", "channel_name")
    chat_type = _first_metadata_value(metadata, "chat_type", "conversation_type", "room_type") or "private"
    if channel == "feishu":
        peer_id = _first_metadata_value(metadata, "peer_id", "open_id", "openId", "sender_id", "user_id", "chat_id")
    else:
        peer_id = _first_metadata_value(
            metadata,
            "peer_id",
            "chat_id",
            "conversation_id",
            "from_user_id",
            "user_id",
            "sender_id",
        )

    has_lane_identity = bool(wechat_account or peer_id)

    if channel != "wechat":
        normalized["channel"] = channel
        normalized["chatgpt_project"] = str(_first_metadata_value(metadata, "chatgpt_project", "project") or "Feishu")
    elif wechat_account:
        normalized["wechat_account"] = str(wechat_account)
        normalized["chatgpt_project"] = str(
            _first_metadata_value(metadata, "chatgpt_project", "project") or f"WeChat-{wechat_account}"
        )
    if has_lane_identity and chat_type:
        normalized["chat_type"] = str(chat_type)
    if peer_id:
        normalized["peer_id"] = str(peer_id)
    if metadata.get("message_id"):
        normalized["message_id"] = str(metadata["message_id"])
    return normalized


def normalize_channel(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"feishu", "lark"}:
        return "feishu"
    if text in {"wechat", "wecom", "weixin"}:
        return "wechat"
    return ""


def _looks_like_feishu(metadata: dict[str, Any]) -> bool:
    message_id = str(metadata.get("message_id") or "").lower()
    return bool(metadata.get("open_id") or message_id.startswith(("openclaw-feishu:", "openclaw-lark:")))


def remember_lane_metadata(metadata: dict[str, Any]) -> None:
    global _recent_lane_metadata_at
    lane_metadata = {
        key: metadata[key]
        for key in ("channel", "wechat_account", "chat_type", "peer_id", "chatgpt_project")
        if metadata.get(key)
    }
    if "peer_id" not in lane_metadata:
        return
    _recent_lane_metadata.clear()
    _recent_lane_metadata.update(lane_metadata)
    _recent_lane_metadata_at = time.monotonic()


def get_recent_lane_metadata() -> dict[str, Any]:
    if not _recent_lane_metadata:
        return {}
    if time.monotonic() - _recent_lane_metadata_at > RECENT_METADATA_WINDOW_SECONDS:
        return {}
    return dict(_recent_lane_metadata)


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def bridge_batch_seconds(details: dict[str, Any] | None = None) -> float:
    base_seconds = _float_env("OPENCLAW_BRIDGE_BATCH_SECONDS", 2.0)
    if details and should_wait_for_followup_media(details):
        return max(base_seconds, _float_env("OPENCLAW_BRIDGE_MEDIA_INTENT_BATCH_SECONDS", 8.0))
    return base_seconds


def bridge_batch_settle_seconds() -> float:
    return _float_env("OPENCLAW_BRIDGE_BATCH_SETTLE_SECONDS", 0.35)


def should_wait_for_followup_media(details: dict[str, Any]) -> bool:
    return bool(details["user_text"] and not details["images"] and MEDIA_INTENT_RE.search(details["user_text"]))


def expected_image_count(details: dict[str, Any]) -> int:
    text = str(details.get("user_text") or "")
    if details.get("images"):
        return len(details["images"])
    if not MEDIA_INTENT_RE.search(text):
        return 0
    expected = 1
    for digit in re.findall(r"([1-4])\s*张", text):
        expected = max(expected, int(digit))
    for marker, count in CN_IMAGE_COUNT.items():
        if f"{marker}张" in text:
            expected = max(expected, count)
    for ordinal, count in (("第一张", 1), ("第二张", 2), ("第三张", 3), ("第四张", 4)):
        if ordinal in text:
            expected = max(expected, count)
    return min(expected, MAX_BRIDGE_IMAGES)


def trace_enabled() -> bool:
    return os.getenv("OPENCLAW_BRIDGE_TRACE", "1").strip().lower() not in {"0", "false", "no", "off"}


def trace_batch_event(
    event: str,
    details: dict[str, Any],
    *,
    key: str = "",
    pending: PendingBatch | None = None,
    wait_seconds: float | None = None,
    result: str = "",
) -> None:
    if not trace_enabled():
        return
    metadata = details.get("metadata") or {}
    image_count = len(pending.images) if pending is not None else len(details.get("images") or [])
    expected_images = pending.expected_images if pending is not None else expected_image_count(details)
    payload = {
        "event": event,
        "request_id": details.get("request_id"),
        "wechat_account": metadata.get("wechat_account"),
        "chat_type": metadata.get("chat_type"),
        "peer_id": metadata.get("peer_id"),
        "message_id": metadata.get("message_id"),
        "batch_key": key,
        "text_len": len(details.get("user_text") or ""),
        "image_count": image_count,
        "expected_images": expected_images,
        "has_media_intent": bool(MEDIA_INTENT_RE.search(details.get("user_text") or "")),
    }
    if wait_seconds is not None:
        payload["wait_seconds"] = round(wait_seconds, 3)
    if pending is not None:
        payload["batch_age_ms"] = int((time.monotonic() - pending.created) * 1000)
    if result:
        payload["result"] = result
    print("bridge_request_trace " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def lane_batch_key(metadata: dict[str, Any]) -> str:
    peer_id = metadata.get("peer_id")
    if not peer_id:
        return ""
    if metadata.get("channel") == "feishu":
        return "feishu:" + str(peer_id)
    return "|".join(
        [
            str(metadata.get("wechat_account") or "default"),
            str(metadata.get("chat_type") or "private"),
            str(peer_id),
        ]
    )


def maybe_batch_request(body: dict[str, Any]) -> dict[str, Any] | str:
    details = request_details(body)
    wait_seconds = bridge_batch_seconds(details)
    if wait_seconds <= 0:
        trace_batch_event("batch_passthrough", details, wait_seconds=wait_seconds, result="batch_disabled")
        return body
    if not (details["user_text"] or details["images"]):
        trace_batch_event("batch_passthrough", details, wait_seconds=wait_seconds, result="empty")
        return body
    if details["metadata"]:
        remember_lane_metadata(details["metadata"])
    key = lane_batch_key(details["metadata"])
    if not key:
        trace_batch_event("batch_passthrough", details, wait_seconds=wait_seconds, result="missing_lane")
        return body

    with _pending_batches_lock:
        pending = _pending_batches.get(key)
        if pending and time.monotonic() <= pending.deadline:
            with pending.condition:
                pending.merge(details)
                trace_batch_event(
                    "batch_merge",
                    details,
                    key=key,
                    pending=pending,
                    wait_seconds=wait_seconds,
                    result="no_reply",
                )
                pending.condition.notify_all()
            return NO_REPLY
        pending = PendingBatch(body, details, wait_seconds)
        _pending_batches[key] = pending
        trace_batch_event("batch_wait", details, key=key, pending=pending, wait_seconds=wait_seconds)

    deadline = pending.deadline
    with pending.condition:
        while True:
            now = time.monotonic()
            next_deadline = deadline
            if pending.has_expected_images():
                next_deadline = min(deadline, pending.updated + bridge_batch_settle_seconds())
            remaining = next_deadline - now
            if remaining <= 0:
                break
            pending.condition.wait(timeout=remaining)

    with _pending_batches_lock:
        if _pending_batches.get(key) is pending:
            del _pending_batches[key]
    trace_batch_event("batch_flush", details, key=key, pending=pending, wait_seconds=wait_seconds)
    return pending.to_body()


def _first_metadata_value(metadata: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = metadata.get(key)
        if value not in (None, ""):
            return value
    return None


def extract_assistant_reply(payload: dict[str, Any]) -> str:
    try:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        return content.strip() if isinstance(content, str) else ""
    except Exception:
        return ""


def normalize_reply(text: str) -> str:
    lowered = text.strip().lower()
    if not lowered:
        return ""
    for pattern in ENGLISH_ERROR_PATTERNS:
        if pattern in lowered:
            return diagnostic_message(
                "bridge -> WebDock -> ChatGPT connected, but ChatGPT/WebDock returned a timeout or browser error.",
                "ChatGPT browser response extraction",
            )
    return text.strip()


def diagnostic_message(reason: str, stop_at: str) -> str:
    return (
        f"{FALLBACK_MESSAGE}\n"
        f"诊断：OpenClaw -> openclaw-bridge 已联通；{reason}\n"
        f"停止点：{stop_at}。"
    )


def parse_http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
    except Exception:
        return str(exc)
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error_code") or exc)
    return str(detail or payload or exc)


def call_webdock(body: dict[str, Any]) -> str:
    outbound = build_webdock_body(body)
    data = json.dumps(outbound, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webdock_url(),
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.getenv("WEB_DOCK_API_TOKEN", ""),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=webdock_timeout()) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return normalize_reply(extract_assistant_reply(payload)) or FALLBACK_MESSAGE


def build_reply(body: dict[str, Any]) -> str:
    user_text = get_last_user_message(body.get("messages"))
    if not webdock_configured():
        return f"已收到你的微信消息：{user_text}" if user_text else "已收到你的微信消息。"
    try:
        batched_body = maybe_batch_request(body)
        if batched_body == NO_REPLY:
            return NO_REPLY
        return call_webdock(batched_body)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return diagnostic_message(
                "bridge -> WebDock 已联通；WebDock 返回 429 BUSY，浏览器正在处理另一条请求。",
                "WebDock browser lock",
            )
        if exc.code in {401, 403}:
            return diagnostic_message(
                f"bridge -> WebDock 已联通；WebDock 拒绝鉴权（HTTP {exc.code}）。",
                "WebDock API token",
            )
        return diagnostic_message(
            f"bridge -> WebDock 已联通；WebDock 返回 HTTP {exc.code}: {parse_http_error_message(exc)}",
            "WebDock API",
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"webdock unavailable: {exc}")
        if isinstance(exc, TimeoutError):
            return diagnostic_message(
                "bridge -> WebDock 请求超时，ChatGPT 可能仍在生成或页面未完成响应。",
                "WebDock/ChatGPT timeout",
            )
        if isinstance(exc, json.JSONDecodeError):
            return diagnostic_message(
                "bridge -> WebDock 已联通，但返回内容不是有效 JSON。",
                "WebDock API response",
            )
        return diagnostic_message(
            f"bridge -> WebDock 未联通或连接失败：{exc}",
            "ECS tunnel or WebDock API",
        )


def keepalive_interval() -> float:
    """Seconds between SSE keepalive chunks while WebDock is still working.

    Must stay well under OpenClaw's ~120s idle timeout so the connection is not
    killed while ChatGPT is still thinking/generating behind WebDock."""
    try:
        return max(1.0, float(os.getenv("OPENCLAW_BRIDGE_KEEPALIVE_SECONDS", "15")))
    except ValueError:
        return 15.0


def _stream_chunk(model: str, *, delta: dict[str, Any], finish_reason: str | None) -> bytes:
    chunk = {
        "id": "chatcmpl-openclaw-bridge",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def stream_sse(
    write: Any,
    body: dict[str, Any],
    model: str,
    *,
    reply_fn: Any = None,
    keepalive: float | None = None,
) -> None:
    """Emit an OpenAI-style SSE stream while build_reply runs in the background.

    build_reply blocks for as long as WebDock/ChatGPT take to answer (can be
    minutes for long reasoning + image work). During that wait we periodically
    push an empty-delta keepalive chunk so OpenClaw's idle timer keeps resetting
    instead of cutting us off at ~120s. ``write(bytes) -> bool`` must return
    False once the client has disconnected, which stops the stream early."""
    if reply_fn is None:
        reply_fn = build_reply
    if keepalive is None:
        keepalive = keepalive_interval()

    result: dict[str, str] = {}

    def _run() -> None:
        try:
            result["reply"] = reply_fn(body)
        except Exception as exc:  # keep the stream alive even if the worker fails
            print(f"bridge stream worker error: {exc}")
            result["reply"] = FALLBACK_MESSAGE

    worker = Thread(target=_run, daemon=True)
    worker.start()

    while True:
        worker.join(timeout=keepalive)
        if not worker.is_alive():
            break
        if not write(_stream_chunk(model, delta={"content": ""}, finish_reason=None)):
            return  # OpenClaw disconnected; drop the (still-running) worker result

    reply = result.get("reply")
    if reply == NO_REPLY:
        write(_stream_chunk(model, delta={}, finish_reason="stop"))
        write(b"data: [DONE]\n\n")
        return
    reply = reply or FALLBACK_MESSAGE
    write(_stream_chunk(model, delta={"content": reply}, finish_reason=None))
    write(_stream_chunk(model, delta={}, finish_reason="stop"))
    write(b"data: [DONE]\n\n")


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proxy_media(self) -> None:
        # Proxy /media/<token> to WebDock so OpenClaw (which can reach this bridge
        # at host.docker.internal but NOT the 127.0.0.1-bound reverse tunnel) can
        # download widget screenshots. /media on WebDock is unauthenticated.
        target = webdock_media_root() + self.path
        try:
            with urllib.request.urlopen(target, timeout=20) as response:
                data = response.read()
                content_type = response.headers.get("Content-Type", "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            try:
                self.wfile.write(f"media proxy error: {exc}".encode("utf-8"))
            except Exception:
                pass

    def do_GET(self) -> None:
        if self.path.startswith("/media/"):
            return self._proxy_media()
        if self.path.rstrip("/") == "/v1/models":
            model = os.getenv("WEB_DOCK_MODEL", "browser-chatgpt" if webdock_configured() else "echo")
            owner = "webdock" if webdock_configured() else "local"
            return self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model", "created": int(time.time()), "owned_by": owner},
                    ],
                },
            )
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self._json(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}

        model = os.getenv("WEB_DOCK_MODEL", body.get("model", "echo")) if webdock_configured() else body.get("model", "echo")

        if body.get("stream"):
            return self._stream_reply(body, model)

        reply = build_reply(body)
        content = "" if reply == NO_REPLY else reply
        return self._json(
            200,
            {
                "id": "chatcmpl-openclaw-bridge",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def _stream_reply(self, body: dict[str, Any], model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def write(data: bytes) -> bool:
            try:
                self.wfile.write(data)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionError, OSError):
                return False

        stream_sse(write, body, model)

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def get_bridge_hosts() -> list[str]:
    hosts = os.getenv("OPENCLAW_BRIDGE_HOSTS") or os.getenv("OPENCLAW_BRIDGE_HOST", "127.0.0.1")
    return [host.strip() for host in hosts.split(",") if host.strip()]


if __name__ == "__main__":
    bridge_hosts = get_bridge_hosts()
    bridge_port = int(os.getenv("OPENCLAW_BRIDGE_PORT", "18080"))
    servers = [ThreadingHTTPServer((host, bridge_port), Handler) for host in bridge_hosts]
    for server in servers[1:]:
        Thread(target=server.serve_forever, daemon=True).start()
    print("OpenClaw bridge listening on " + ", ".join(f"http://{host}:{bridge_port}/v1" for host in bridge_hosts))
    servers[0].serve_forever()
