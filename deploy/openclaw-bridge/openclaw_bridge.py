#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import hmac
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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Condition, Lock, Thread
from typing import Any


def utc_now_iso() -> str:
    """Current time as UTC ISO8601 with milliseconds and a Z suffix, e.g.
    ``2026-06-23T15:30:00.123Z`` — every bridge log line shows its timezone at a
    glance (containers run UTC, but the suffix makes it explicit and unambiguous)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log_line(message: str) -> None:
    """Print a diagnostic line prefixed with a UTC ISO8601-Z timestamp."""
    print(f"{utc_now_iso()} {message}", flush=True)


FALLBACK_MESSAGE = os.getenv("WEB_DOCK_FALLBACK_MESSAGE", "ChatGPT 浏览器暂不可用，请稍后再试。")
NO_REPLY = "__OPENCLAW_BRIDGE_NO_REPLY__"
OPENCLAW_METADATA_PREFIX_RE = re.compile(
    r"^(?:\[[^\]\n]*(?:UTC|GMT(?:[+-]\d{1,2}(?::\d{2})?)?)[^\]\n]*\]\s*)?"
    r"(?:Conversation info|Sender) \(untrusted metadata\):\s*",
    flags=re.DOTALL,
)
OPENCLAW_MESSAGE_ID_LINE_RE = re.compile(r"^[ \t]*\[message_id:[^\]\n]*\][ \t]*\n?", re.IGNORECASE | re.MULTILINE)
OPENCLAW_MESSAGE_ID_CAPTURE_RE = re.compile(r"\[message_id:\s*([^\]\n]+)\]", re.IGNORECASE)
FEISHU_MENTION_HELPER_PREFIXES = (
    "[System: The content may include mention tags in the form ",
    "[System: If user_id is ",
)
try:
    # Inbound images forwarded to WebDock per turn. Env-tunable (default 20) so the
    # cap can track what ChatGPT's composer actually accepts without a code change.
    MAX_BRIDGE_IMAGES = max(1, int(os.getenv("MAX_BRIDGE_IMAGES", "20")))
except ValueError:
    MAX_BRIDGE_IMAGES = 20
MAX_BRIDGE_IMAGE_BYTES = 20 * 1024 * 1024
FEISHU_PEER_PREFIXES = ("ou_", "oc_")

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
OPENCLAW_MEDIA_ATTACHED_LINE_RE = re.compile(r"^[ \t]*\[media attached[^\]\n]*\][ \t]*$", re.MULTILINE)
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
# Inline image placeholder OpenClaw appends to the text for each attached image,
# e.g. "![image]" or "![image](media://…)". The real image is forwarded as an
# image_url part, so the placeholder is noise in the ChatGPT prompt.
OPENCLAW_IMAGE_PLACEHOLDER_RE = re.compile(r"!\[[^\]\n]*\](\([^)\n]*\))?")
# OpenClaw's newer inline placeholder for a forwarded attachment, e.g.
# "<media:document> (房屋租赁合同_简易版.pdf)". Unmatched until now, so it reached
# ChatGPT verbatim and showed up beside the attachment pill as a stray tag
# (observed 2026-07-27). The parenthesised name is the ONLY place the original
# filename survives — WebDock uploads under a generated temp name — so it is
# rewritten, not dropped.
OPENCLAW_MEDIA_TAG_RE = re.compile(
    r"<media:[a-z_]+>[ \t]*(?:\((?P<name>[^)\n]*)\))?", re.IGNORECASE
)
FILE_MARKER_RE = re.compile(
    r"^[ \t]*FILE:\s+(?P<url>\S+)\s+name=(?P<name>\S+)\s+mime=(?P<mime>\S+)[ \t]*$",
    re.MULTILINE,
)
MEDIA_MARKER_RE = re.compile(
    r"^[ \t]*MEDIA:\s+(?P<url>\S+)[ \t]*$",
    re.MULTILINE,
)
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
_feishu_tenant_token: str = ""
_feishu_tenant_token_expires_at = 0.0
_feishu_group_policy_cache: dict[str, tuple[float, bool, str]] = {}
_feishu_group_policy_cache_lock = Lock()
# Cache TTL for the bitable-driven group policy lookup. Default 600s (10min) —
# safe to raise (e.g. 3600+) once the bitable automation -> /admin/invalidate-
# feishu-group-policy webhook is wired up, since manual edits then push-invalidate
# the cache instead of waiting it out. Env override lets ops tune without rebuild.
FEISHU_GROUP_POLICY_CACHE_SECONDS = float(
    os.getenv("OPENCLAW_BRIDGE_FEISHU_POLICY_CACHE_SECONDS", "600")
)


class WebDockResult:
    def __init__(
        self,
        reply: str,
        metadata: dict[str, Any] | None = None,
        footer: dict[str, Any] | None = None,
    ) -> None:
        self.reply = reply
        self.metadata = metadata or {}
        # Developer-facing delivery info (device/route/elapsed) for the card footer;
        # kept out of `metadata` so it never leaks into session/bitable records.
        self.footer = footer or {}


class PendingBatch:
    def __init__(self, body: dict[str, Any], details: dict[str, Any], wait_seconds: float) -> None:
        self.condition = Condition()
        self.body = body
        self.user_text = details["user_text"]
        self.images = list(details["images"])
        self.metadata = dict(details["metadata"])
        self.raw_metadata = dict(details.get("raw_metadata") or {})
        self.request_id = str(details["request_id"])
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
        for key, value in (details.get("raw_metadata") or {}).items():
            if value and not self.raw_metadata.get(key):
                self.raw_metadata[key] = value
        self.updated = time.monotonic()

    def has_text_and_images(self) -> bool:
        return bool(self.user_text and self.images)

    def has_expected_images(self) -> bool:
        return self.has_text_and_images() and len(self.images) >= max(1, self.expected_images)

    def to_body(self) -> dict[str, Any]:
        body = dict(self.body)
        body["_bridge_request_id"] = self.request_id
        body["messages"] = [{"role": "user", "content": build_outbound_content(self.user_text, self.images)}]
        metadata = dict(self.raw_metadata)
        metadata.update(self.metadata)
        if metadata:
            body["metadata"] = metadata
        return body


def clean_user_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    _metadata, metadata_end = parse_openclaw_metadata_prefixes(text)
    cleaned = text[metadata_end:] if metadata_end else text
    cleaned = OPENCLAW_MESSAGE_ID_LINE_RE.sub("", cleaned)
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


def replace_media_tag_placeholders(text: str) -> str:
    """Rewrite ``<media:document> (name.pdf)`` into the same note a binary <file>
    block gets, keeping the real filename that would otherwise be lost."""
    def _repl(match: "re.Match[str]") -> str:
        name = (match.group("name") or "").strip()
        return f"（已上传文件：{name}）" if name else "（已上传文件）"
    return OPENCLAW_MEDIA_TAG_RE.sub(_repl, text)


def strip_openclaw_media_helper_text(text: str) -> str:
    text = OPENCLAW_MEDIA_ATTACHED_LINE_RE.sub("", text)
    text = OPENCLAW_MEDIA_NO_CAPTION_RE.sub("", text)
    text = replace_media_tag_placeholders(text)
    text = OPENCLAW_IMAGE_PLACEHOLDER_RE.sub("", text)
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
    metadata, _metadata_end = parse_openclaw_metadata_prefixes(text)
    return metadata


def parse_openclaw_metadata_prefixes(text: str) -> tuple[dict[str, Any], int]:
    combined: dict[str, Any] = {}
    pos = 0
    while pos < len(text):
        metadata, metadata_end = parse_openclaw_metadata_prefix(text[pos:])
        if not metadata_end:
            break
        combined.update(metadata)
        pos += metadata_end
    return combined, pos


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
    be http(s) or base64 data URLs. Text-only content yields nothing.

    A single inbound image must yield exactly ONE part. OpenClaw (Feishu) may
    annotate the same attachment BOTH as a `[media attached: …]` text ref AND a
    separate image_url part, which previously produced two copies. Inbound
    text-refs resolve to data URLs from the mounted inbound dir and are always
    fetchable by WebDock, so when both sources are present we keep the text-ref
    parts and drop the image_url parts; when no text-ref resolves we fall back to
    the image_url parts (e.g. channels that only ever send image_url)."""
    if isinstance(content, str):
        return extract_openclaw_media_image_parts(content)
    if not isinstance(content, list):
        return []
    text_ref_parts: list[dict[str, Any]] = []
    image_url_parts: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in content:
        if not isinstance(item, dict):
            continue
        for key in ("text", "content"):
            value = item.get(key)
            if isinstance(value, str):
                for part in extract_openclaw_media_image_parts(value):
                    url = part["image_url"]["url"]
                    if url not in seen_urls:
                        seen_urls.add(url)
                        text_ref_parts.append(part)
        image_url = item.get("image_url")
        if image_url is None:
            continue
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url, str) and url.strip() and url.strip() not in seen_urls:
            seen_urls.add(url.strip())
            image_url_parts.append({"type": "image_url", "image_url": {"url": url.strip()}})
    parts = text_ref_parts if text_ref_parts else image_url_parts
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
    # Must outlast WebDock's 20-minute hard cap; SSE keepalives cover OpenClaw's
    # shorter idle limit while this synchronous upstream request is active.
    try:
        return max(1260, int(os.getenv("WEB_DOCK_TIMEOUT_SECONDS", "1260")))
    except ValueError:
        return 1260


def request_details(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages")
    user_text = get_last_user_message(messages)
    if isinstance(body.get("_bridge_user_text"), str):
        user_text = body["_bridge_user_text"].strip()
    images = get_last_user_images(messages)
    raw_metadata = collect_request_metadata(body)
    if not _first_metadata_value(raw_metadata, "message_id", "messageId", "msgid"):
        raw_text = get_last_user_raw_text(messages)
        match = OPENCLAW_MESSAGE_ID_CAPTURE_RE.search(raw_text)
        if match:
            raw_metadata["message_id"] = match.group(1).strip()
    metadata = build_webdock_metadata(body)
    if not metadata.get("message_id"):
        message_id = _first_metadata_value(raw_metadata, "message_id", "messageId", "msgid")
        if message_id:
            metadata["message_id"] = str(message_id)
    if metadata.get("channel") == "feishu":
        user_text = _strip_feishu_sender_prefix(user_text, get_last_user_metadata(messages))
        user_text = strip_feishu_mention_helper_text(user_text)
        enrich_feishu_metadata_with_session_route(metadata, raw_metadata, user_text)
        chat_mode = feishu_chat_mode({"metadata": metadata, "raw_metadata": raw_metadata})
        if chat_mode:
            metadata["chatgpt_mode"] = chat_mode
    elif metadata.get("channel") == "wecom":
        user_text = strip_wecom_bot_mention(user_text)
    if not metadata.get("peer_id"):
        inherited_metadata = get_recent_lane_metadata()
        if inherited_metadata:
            inherited_metadata.update(metadata)
            metadata = inherited_metadata
    request_id = str(body.get("_bridge_request_id") or "").strip()
    if not request_id:
        message_id = _first_metadata_value(raw_metadata, "message_id", "messageId", "msgid")
        if message_id:
            request_id = hashlib.sha256(f"openclaw:{message_id}".encode("utf-8")).hexdigest()[:24]
        else:
            request_id = uuid.uuid4().hex[:24]
        body["_bridge_request_id"] = request_id
    return {
        "request_id": request_id,
        "user_text": user_text,
        "images": images,
        "metadata": metadata,
        "raw_metadata": raw_metadata,
    }


def build_webdock_body(body: dict[str, Any]) -> dict[str, Any]:
    details = request_details(body)
    outbound = {
        "model": os.getenv("WEB_DOCK_MODEL", "browser-chatgpt"),
        "messages": [{"role": "user", "content": build_outbound_content(details["user_text"], details["images"])}],
        "stream": False,
    }
    metadata = dict(details["metadata"])
    metadata["request_id"] = details["request_id"]
    if metadata:
        outbound["metadata"] = metadata
        remember_lane_metadata(details["metadata"])
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
    metadata = collect_request_metadata(body)

    normalized: dict[str, Any] = {}
    channel = normalize_channel(
        _first_metadata_value(metadata, "channel", "platform", "source", "adapter", "provider", "surface", "originatingChannel")
    )
    if not channel and _looks_like_feishu(metadata):
        channel = "feishu"
    if not channel and _looks_like_wecom(metadata):
        channel = "wecom"
    channel = channel or "wechat"
    channel_account = _first_metadata_value(
        metadata,
        "wechat_account",
        "account_id",
        "accountId",
        "account",
        "channel_id",
        "channel_name",
    )
    chat_type = _first_metadata_value(metadata, "chat_type", "chatType", "conversation_type", "room_type") or "private"
    if channel == "feishu":
        chat_type = _infer_feishu_chat_type(metadata, chat_type)
    elif channel == "wecom":
        chat_type = _infer_wecom_chat_type(metadata, chat_type)
    if channel == "feishu":
        peer_id = _feishu_lane_peer_id(metadata, chat_type)
    elif channel == "wecom":
        peer_id = _wecom_lane_peer_id(metadata, chat_type)
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

    has_lane_identity = bool(channel_account or peer_id)

    if channel == "wecom":
        normalized["channel"] = "wecom"
        normalized["wechat_account"] = str(channel_account or "company-b")
        normalized["chatgpt_project"] = str(
            _first_metadata_value(metadata, "chatgpt_project", "project") or "WeCom"
        )
    elif channel != "wechat":
        normalized["channel"] = channel
        normalized["chatgpt_project"] = str(_first_metadata_value(metadata, "chatgpt_project", "project") or "Feishu")
    elif channel_account:
        normalized["wechat_account"] = str(channel_account)
        normalized["chatgpt_project"] = str(
            _first_metadata_value(metadata, "chatgpt_project", "project") or f"WeChat-{channel_account}"
        )
    if has_lane_identity and chat_type:
        normalized["chat_type"] = str(chat_type)
    if peer_id:
        normalized["peer_id"] = str(peer_id)
    message_id = _first_metadata_value(metadata, "message_id", "messageId", "msgid")
    if message_id:
        normalized["message_id"] = str(message_id)
    return normalized


def enrich_feishu_metadata_with_session_route(
    metadata: dict[str, Any],
    raw_metadata: dict[str, Any],
    user_text: str,
) -> None:
    session_key = feishu_session_key_from_metadata(metadata, raw_metadata)
    details = {"metadata": metadata, "raw_metadata": raw_metadata, "user_text": user_text}
    is_new_conversation = feishu_command_type(user_text) == "/新对话"
    current_record: dict[str, Any] | None = None
    if session_key:
        try:
            current_record = find_current_feishu_session_record(session_key)
        except Exception as exc:
            log_line(
                "feishu_session_route_lookup_failed "
                + json.dumps({"session_key": session_key, "error": str(exc)}, ensure_ascii=False, sort_keys=True)
            )
    fields = current_record.get("fields") if isinstance(current_record, dict) else {}
    session_url = session_name = ""
    if isinstance(fields, dict):
        session_url = bitable_url_text(fields.get("ChatGPT 项目首页链接")) or project_home_from_conversation_url(
            bitable_field_text(fields.get("ChatGPT 对话链接"))
        )
        session_name = bitable_field_text(fields.get("ChatGPT 项目名"))
    peer_url, peer_name = feishu_peer_chatgpt_project_config(details)
    global_url, global_name = feishu_global_chatgpt_project_config()
    # 继续旧对话时留在原会话的项目里（会话记录 > 群/用户配置 > 全局规则 > env）；
    # /新对话 时群/用户配置优先（群/用户配置 > 会话记录 > 全局规则 > env），
    # 否则旧会话记录会把「默认新对话项目链接」永远压住，改配置无法生效。
    if is_new_conversation:
        candidates = [(peer_url, peer_name), (session_url, session_name), (global_url, global_name)]
    else:
        candidates = [(session_url, session_name), (peer_url, peer_name), (global_url, global_name)]
    project_url, project_name = next(
        ((url, name) for url, name in candidates if url), ("", "")
    )
    if not project_name:
        project_name = next((name for _, name in candidates if name), "")
    if project_url:
        metadata["chatgpt_project_url"] = project_url
    current_project = str(metadata.get("chatgpt_project") or "").strip()
    if project_name and current_project in {"", "Feishu"}:
        metadata["chatgpt_project"] = project_name

    conversation_url = ""
    if isinstance(fields, dict):
        conversation_url = bitable_field_text(fields.get("ChatGPT 对话链接"))
    if not conversation_url:
        return
    if feishu_command_type(user_text) == "/新对话":
        metadata["previous_chatgpt_conversation_url"] = conversation_url
    else:
        metadata["chatgpt_conversation_url"] = conversation_url


def collect_request_metadata(body: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if isinstance(body.get("metadata"), dict):
        metadata.update(body["metadata"])
    metadata.update(get_last_user_metadata(body.get("messages")))
    return metadata


def normalize_channel(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"feishu", "lark"}:
        return "feishu"
    if text in {"wecom", "qywx", "wework", "enterprise-wechat"}:
        return "wecom"
    if text in {"wechat", "weixin"}:
        return "wechat"
    return ""


def _strip_lane_peer_prefix(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    for prefix in ("user:", "group:", "chat:", "open_id:", "openid:"):
        if lowered.startswith(prefix):
            return text[len(prefix):]
    return text


def _feishu_lane_peer_id(metadata: dict[str, Any], chat_type: Any) -> str:
    raw_peer = _first_metadata_value(metadata, "peer_id", "id")
    chat_id = _first_metadata_value(metadata, "chat_id", "chatId", "conversation_id", "room_id")
    open_id = _first_metadata_value(metadata, "open_id", "openId", "sender_id", "user_id", "from_user_id")

    prefixed = _canonical_feishu_prefixed_peer(raw_peer)
    if _is_group_chat(chat_type):
        if chat_id:
            return _feishu_group_peer(chat_id)
        if prefixed.startswith("group:"):
            return prefixed
        if raw_peer and not prefixed.startswith("user:"):
            return _feishu_group_peer(raw_peer)
    if prefixed:
        return prefixed
    if open_id:
        return _feishu_user_peer(open_id)
    if chat_id:
        return _feishu_group_peer(chat_id)
    return ""


def _canonical_feishu_prefixed_peer(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered.startswith("group_user:"):
        return text
    if lowered.startswith("group:"):
        return _feishu_group_peer(text[len("group:"):])
    if lowered.startswith("chat:"):
        return _feishu_group_peer(text[len("chat:"):])
    if lowered.startswith("user:"):
        return _feishu_user_peer(text[len("user:"):])
    if lowered.startswith("open_id:"):
        return _feishu_user_peer(text[len("open_id:"):])
    if lowered.startswith("openid:"):
        return _feishu_user_peer(text[len("openid:"):])
    return ""


def _feishu_user_peer(value: Any) -> str:
    peer = _strip_lane_peer_prefix(value)
    return f"user:{peer}" if peer else ""


def _feishu_group_peer(value: Any) -> str:
    peer = _strip_lane_peer_prefix(value)
    return f"group:{peer}" if peer else ""


def _is_group_chat(chat_type: Any) -> bool:
    return str(chat_type or "").strip().lower() in {"group", "chat", "group_chat", "room"}


def _infer_feishu_chat_type(metadata: dict[str, Any], chat_type: Any) -> str:
    if _is_group_chat(chat_type):
        return "group"
    if metadata.get("is_group_chat") is True:
        return "group"
    if str(metadata.get("is_group_chat") or "").strip().lower() in {"1", "true", "yes"}:
        return "group"
    return str(chat_type or "private")


def _looks_like_wecom(metadata: dict[str, Any]) -> bool:
    chat_id = str(_first_metadata_value(metadata, "chat_id", "chatId") or "").strip().lower()
    if chat_id.startswith("wecom:"):
        return True
    label = str(metadata.get("conversation_label") or "").strip().lower()
    return label.startswith(("wecom:", "group:wecom:"))


def _infer_wecom_chat_type(metadata: dict[str, Any], chat_type: Any) -> str:
    if _is_group_chat(chat_type):
        return "group"
    if str(metadata.get("is_group_chat") or "").strip().lower() in {"1", "true", "yes"}:
        return "group"
    if str(metadata.get("conversation_label") or "").strip().lower().startswith("group:"):
        return "group"
    return "private"


def _strip_wecom_transport_prefix(value: Any) -> str:
    text = str(value or "").strip()
    return text[len("wecom:"):] if text.lower().startswith("wecom:") else text


def _wecom_lane_peer_id(metadata: dict[str, Any], chat_type: Any) -> str:
    chat_id = _strip_wecom_transport_prefix(
        _first_metadata_value(metadata, "chat_id", "chatId", "conversation_id")
    )
    sender_id = _strip_wecom_transport_prefix(
        _first_metadata_value(metadata, "sender_id", "user_id", "from_user_id", "id")
    )
    if _is_group_chat(chat_type):
        peer = chat_id or _strip_lane_peer_prefix(metadata.get("peer_id"))
        return f"group:{_strip_lane_peer_prefix(peer)}" if peer else ""
    peer = sender_id or chat_id or _strip_lane_peer_prefix(metadata.get("peer_id"))
    return f"user:{_strip_lane_peer_prefix(peer)}" if peer else ""


def strip_wecom_bot_mention(text: str) -> str:
    """Remove only the official callback's leading or trailing self mention."""
    if not text:
        return text
    bot_name = os.getenv("WECOM_ASSISTANT_NAME", "统一 AI 助手").strip() or "统一 AI 助手"
    flexible_name = r"\s*".join(re.escape(part) for part in bot_name.split())
    cleaned = re.sub(
        rf"^\s*@\s*{flexible_name}(?:[ \t]+|\r?\n+|$)",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return re.sub(
        rf"[ \t]*@\s*{flexible_name}\s*$",
        "",
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    ).strip()


def _strip_feishu_sender_prefix(text: str, raw_metadata: dict[str, Any]) -> str:
    """OpenClaw prefixes Feishu DM text with '<sender name>: '. Strip it (only when
    it matches the known sender) so leading triggers like '/新对话' work and the
    prompt isn't polluted. Never strips an arbitrary 'word: '."""
    if not isinstance(text, str) or not text:
        return text
    names: list[str] = []
    for key in ("name", "label"):
        raw = str(raw_metadata.get(key) or "").strip()
        if raw:
            names.append(raw)
            names.append(raw.split(" (")[0].strip())  # "hao (ou_…)" -> "hao"
    for name in names:
        if not name:
            continue
        prefix = f"{name}: "
        if text.startswith(prefix):
            return text[len(prefix):].lstrip()
    return text


def strip_feishu_mention_helper_text(text: str) -> str:
    """Remove OpenClaw's Feishu mention instructions and the bot's own leading
    mention. The helper block is channel-injected text, not user content."""
    if not text:
        return text
    kept: list[str] = []
    saw_self_mention_helper = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(FEISHU_MENTION_HELPER_PREFIXES):
            if stripped.startswith("[System: If user_id is ") and stripped.endswith("that mention refers to you.]"):
                saw_self_mention_helper = True
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    if saw_self_mention_helper:
        cleaned = re.sub(r"^\s*<at\s+user_id=([\"'])[^\"']+\1>.*?</at>\s*", "", cleaned, count=1)
        cleaned = re.sub(r"^\s*@\S+(?:[ \t]+|(?=\r?$))", "", cleaned, count=1)
    return cleaned.strip()


def _looks_like_feishu(metadata: dict[str, Any]) -> bool:
    if metadata.get("open_id"):
        return True
    message_id = str(metadata.get("message_id") or "").lower()
    if message_id.startswith(("openclaw-feishu:", "openclaw-lark:", "om_")):
        return True
    for key in (
        "peer_id",
        "open_id",
        "openId",
        "sender_id",
        "user_id",
        "chat_id",
        "from_user_id",
        "conversation_id",
        "id",
    ):
        candidate = _strip_lane_peer_prefix(metadata.get(key)).lower()
        if candidate.startswith(FEISHU_PEER_PREFIXES):
            return True
    return False



def feishu_api_base() -> str:
    return (
        os.getenv("FEISHU_API_BASE")
        or os.getenv("FEISHU_COMPANY_A_API_BASE")
        or "https://open.feishu.cn/open-apis"
    ).rstrip("/")


def feishu_app_credentials() -> tuple[str, str]:
    return (
        os.getenv("FEISHU_APP_ID") or os.getenv("FEISHU_COMPANY_A_APP_ID") or "",
        os.getenv("FEISHU_APP_SECRET") or os.getenv("FEISHU_COMPANY_A_APP_SECRET") or "",
    )


def feishu_session_console_app_token() -> str:
    return (
        os.getenv("FEISHU_SESSION_CONSOLE_APP_TOKEN")
        or os.getenv("FEISHU_COMPANY_A_SESSION_CONSOLE_APP_TOKEN")
        or ""
    )


def system_config_app_token() -> str:
    """独立「系统配置」多维表格的 app_token；缺失返回空（不回退会话台）。"""
    return os.getenv("FEISHU_SYSTEM_CONFIG_APP_TOKEN") or ""


def system_config_table_id(name: str) -> str:
    """按域名取「系统配置」簿的 table_id：FEISHU_SYSTEM_CONFIG_<NAME>_TABLE_ID。"""
    return os.getenv(f"FEISHU_SYSTEM_CONFIG_{name.upper()}_TABLE_ID") or ""


def feishu_session_console_table_id(kind: str) -> str:
    env_names = {
        "message": (
            "FEISHU_SESSION_CONSOLE_MESSAGE_TABLE_ID",
            "FEISHU_COMPANY_A_SESSION_CONSOLE_MESSAGE_TABLE_ID",
        ),
        "task": (
            "FEISHU_SESSION_CONSOLE_TASK_TABLE_ID",
            "FEISHU_COMPANY_A_SESSION_CONSOLE_TASK_TABLE_ID",
        ),
        "session": (
            "FEISHU_SESSION_CONSOLE_SESSION_TABLE_ID",
            "FEISHU_COMPANY_A_SESSION_CONSOLE_SESSION_TABLE_ID",
        ),
        "user": (
            "FEISHU_SESSION_CONSOLE_USER_TABLE_ID",
            "FEISHU_COMPANY_A_SESSION_CONSOLE_USER_TABLE_ID",
        ),
        "group": (
            "FEISHU_SESSION_CONSOLE_GROUP_TABLE_ID",
            "FEISHU_COMPANY_A_SESSION_CONSOLE_GROUP_TABLE_ID",
        ),
        "rule": (
            "FEISHU_SESSION_CONSOLE_RULE_TABLE_ID",
            "FEISHU_COMPANY_A_SESSION_CONSOLE_RULE_TABLE_ID",
        ),
    }.get(kind, ())
    for name in env_names:
        value = os.getenv(name)
        if value:
            return value
    return ""


def feishu_session_console_configured() -> bool:
    app_id, app_secret = feishu_app_credentials()
    return bool(
        app_id
        and app_secret
        and feishu_session_console_app_token()
        and feishu_session_console_table_id("message")
    )


def feishu_session_index_configured() -> bool:
    app_id, app_secret = feishu_app_credentials()
    return bool(app_id and app_secret and feishu_session_console_app_token() and feishu_session_console_table_id("session"))


def feishu_default_chatgpt_project_url() -> str:
    for key in (
        "FEISHU_SESSION_CONSOLE_CHATGPT_PROJECT_URL",
        "FEISHU_COMPANY_A_SESSION_CONSOLE_CHATGPT_PROJECT_URL",
        "FEISHU_CHATGPT_PROJECT_URL",
        "FEISHU_COMPANY_A_CHATGPT_PROJECT_URL",
        "CHATGPT_PROJECT_URL",
    ):
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return ""


def feishu_default_chatgpt_project_name() -> str:
    return feishu_env_chatgpt_project_name() or "飞书 AI 会话台"


def feishu_env_chatgpt_project_name() -> str:
    for key in (
        "FEISHU_SESSION_CONSOLE_CHATGPT_PROJECT_NAME",
        "FEISHU_COMPANY_A_SESSION_CONSOLE_CHATGPT_PROJECT_NAME",
        "FEISHU_CHATGPT_PROJECT_NAME",
        "FEISHU_COMPANY_A_CHATGPT_PROJECT_NAME",
    ):
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return ""


def feishu_tenant_access_token() -> str:
    global _feishu_tenant_token, _feishu_tenant_token_expires_at
    if _feishu_tenant_token and time.monotonic() < _feishu_tenant_token_expires_at:
        return _feishu_tenant_token
    app_id, app_secret = feishu_app_credentials()
    if not app_id or not app_secret:
        return ""
    response = feishu_post_json(
        "/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
        auth_token="",
    )
    token = str(response.get("tenant_access_token") or "")
    if not token:
        raise RuntimeError(f"Feishu tenant_access_token missing: {response}")
    try:
        ttl = max(60, int(response.get("expire")) - 120)
    except (TypeError, ValueError):
        ttl = 6000
    _feishu_tenant_token = token
    _feishu_tenant_token_expires_at = time.monotonic() + ttl
    return token


def feishu_request_json(
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    auth_token: str | None = None,
    method: str = "POST",
) -> dict[str, Any]:
    method = method.upper()
    data = (
        json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        if method not in ("GET", "DELETE")
        else None
    )
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if auth_token is None:
        auth_token = feishu_tenant_access_token()
    if auth_token:
        headers["Authorization"] = "Bearer " + auth_token
    request = urllib.request.Request(
        feishu_api_base() + path,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    code = result.get("code") if isinstance(result, dict) else None
    if code not in (None, 0):
        raise RuntimeError(f"Feishu API error {code}: {result}")
    return result


def feishu_post_json(
    path: str,
    payload: dict[str, Any],
    *,
    auth_token: str | None = None,
    method: str = "POST",
) -> dict[str, Any]:
    return feishu_request_json(path, payload, auth_token=auth_token, method=method)


def feishu_get_json(path: str, *, auth_token: str | None = None) -> dict[str, Any]:
    return feishu_request_json(path, None, auth_token=auth_token, method="GET")


# Feishu im/v1/files limit (channel doc mediaMaxMb default 30).
FEISHU_MAX_FILE_BYTES = 30 * 1024 * 1024


def feishu_file_type_for(file_name: str) -> str:
    """Map a filename extension to a Feishu im/v1/files ``file_type``. Unknown
    document types fall back to the generic ``stream`` type."""
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return {
        "pdf": "pdf",
        "doc": "doc", "docx": "doc",
        "xls": "xls", "xlsx": "xls",
        "ppt": "ppt", "pptx": "ppt",
        "mp4": "mp4", "opus": "opus",
    }.get(ext, "stream")


def feishu_upload_file(data: bytes, file_name: str, file_type: str, auth_token: str) -> str:
    """Upload bytes to Feishu ``im/v1/files`` (multipart) and return the file_key."""
    boundary = "----openclawBridge" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in (("file_type", file_type), ("file_name", file_name)):
        parts.append(b"--" + boundary.encode())
        parts.append(('Content-Disposition: form-data; name="%s"' % name).encode("utf-8"))
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))
    parts.append(b"--" + boundary.encode())
    parts.append(('Content-Disposition: form-data; name="file"; filename="%s"' % file_name).encode("utf-8"))
    parts.append(b"Content-Type: application/octet-stream")
    parts.append(b"")
    body = crlf.join(parts) + crlf + data + crlf + b"--" + boundary.encode() + b"--" + crlf
    request = urllib.request.Request(
        feishu_api_base() + "/im/v1/files",
        data=body,
        headers={
            "Authorization": "Bearer " + auth_token,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") not in (None, 0):
        raise RuntimeError(f"Feishu file upload error: {result}")
    file_key = (result.get("data") or {}).get("file_key")
    if not file_key:
        raise RuntimeError(f"Feishu file upload missing file_key: {result}")
    return str(file_key)


def feishu_upload_image(data: bytes, auth_token: str) -> str:
    """Upload image bytes to Feishu ``im/v1/images`` and return the image_key."""
    boundary = "----openclawBridge" + uuid.uuid4().hex
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
        feishu_api_base() + "/im/v1/images",
        data=body,
        headers={
            "Authorization": "Bearer " + auth_token,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") not in (None, 0):
        raise RuntimeError(f"Feishu image upload error: {result}")
    image_key = (result.get("data") or {}).get("image_key")
    if not image_key:
        raise RuntimeError(f"Feishu image upload missing image_key: {result}")
    return str(image_key)


def feishu_send_file_message(details: dict[str, Any], message_id: str, file_key: str, auth_token: str) -> None:
    """Deliver a file_key to the Feishu user, replying to their message when we have
    its id, otherwise sending a fresh message to the open_id (DM) / chat_id (group)."""
    content = json.dumps({"file_key": file_key}, ensure_ascii=False)
    if message_id:
        feishu_post_json(
            f"/im/v1/messages/{urllib.parse.quote(message_id)}/reply",
            {"msg_type": "file", "content": content},
            auth_token=auth_token,
        )
        return
    if feishu_is_group_message(details):
        receive_id, receive_id_type = feishu_chat_id(details), "chat_id"
    else:
        receive_id, receive_id_type = feishu_open_id(details), "open_id"
    if not receive_id:
        raise RuntimeError("no Feishu receive_id for file delivery")
    feishu_post_json(
        f"/im/v1/messages?receive_id_type={receive_id_type}",
        {"receive_id": receive_id, "msg_type": "file", "content": content},
        auth_token=auth_token,
    )


def feishu_send_image_message(details: dict[str, Any], message_id: str, image_key: str, auth_token: str) -> None:
    """Deliver an image_key as a native Feishu image message."""
    content = json.dumps({"image_key": image_key}, ensure_ascii=False)
    if message_id:
        feishu_post_json(
            f"/im/v1/messages/{urllib.parse.quote(message_id)}/reply",
            {"msg_type": "image", "content": content},
            auth_token=auth_token,
        )
        return
    if feishu_is_group_message(details):
        receive_id, receive_id_type = feishu_chat_id(details), "chat_id"
    else:
        receive_id, receive_id_type = feishu_open_id(details), "open_id"
    if not receive_id:
        raise RuntimeError("no Feishu receive_id for image delivery")
    feishu_post_json(
        f"/im/v1/messages?receive_id_type={receive_id_type}",
        {"receive_id": receive_id, "msg_type": "image", "content": content},
        auth_token=auth_token,
    )


def feishu_send_interactive_message(details: dict[str, Any], message_id: str, card: dict[str, Any], auth_token: str) -> str:
    """Deliver an interactive card (text + images interleaved) as one message,
    replying to the user's message when we have its id. Returns the created
    message's id ("" if unavailable)."""
    content = json.dumps(card, ensure_ascii=False)
    if message_id:
        resp = feishu_post_json(
            f"/im/v1/messages/{urllib.parse.quote(message_id)}/reply",
            {"msg_type": "interactive", "content": content},
            auth_token=auth_token,
        )
        return str((resp.get("data") or {}).get("message_id") or "")
    if feishu_is_group_message(details):
        receive_id, receive_id_type = feishu_chat_id(details), "chat_id"
    else:
        receive_id, receive_id_type = feishu_open_id(details), "open_id"
    if not receive_id:
        raise RuntimeError("no Feishu receive_id for card delivery")
    resp = feishu_post_json(
        f"/im/v1/messages?receive_id_type={receive_id_type}",
        {"receive_id": receive_id, "msg_type": "interactive", "content": content},
        auth_token=auth_token,
    )
    return str((resp.get("data") or {}).get("message_id") or "")


def feishu_patch_card(message_id: str, card: dict[str, Any], auth_token: str, *, _from_rotation: bool = False) -> None:
    """Update an already-sent interactive card in place (the placeholder becomes the
    final answer). Requires the card to have been sent with config.update_multi=true.
    非轮播调用先掐掉该消息上的占位轮播，保证终局内容不被轮播覆盖。"""
    if not _from_rotation:
        stop_placeholder_rotation(message_id)
    feishu_post_json(
        f"/im/v1/messages/{urllib.parse.quote(message_id)}",
        {"content": json.dumps(card, ensure_ascii=False)},
        auth_token=auth_token,
        method="PATCH",
    )


def feishu_put_card(details: dict[str, Any], card: dict[str, Any], auth_token: str) -> None:
    """Deliver a card: patch the pending processing-card placeholder if one exists,
    otherwise send a fresh reply. Patch failure degrades to a fresh reply so the
    answer is never lost."""
    placeholder_id = details.get("feishu_placeholder_msg_id")
    if placeholder_id:
        try:
            feishu_patch_card(placeholder_id, card, auth_token)
            return
        except Exception as exc:
            log_line(f"feishu card patch failed, sending new reply: {exc}")
    feishu_send_interactive_message(details, feishu_message_id(details), card, auth_token)


def fetch_outbound_file_bytes(url: str) -> bytes:
    """Fetch a file referenced by a FILE marker. WebDock ``/media/<token>`` URLs are
    pulled over the internal WebDock base (reverse tunnel) rather than the public
    host so delivery does not depend on external DNS/TLS."""
    parsed = urllib.parse.urlparse(url)
    target = url
    if parsed.path.startswith("/media/"):
        root = webdock_media_root()
        if root:
            target = root + parsed.path
    with urllib.request.urlopen(target, timeout=30) as response:
        return response.read()


def create_feishu_bitable_record(table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    app_token = feishu_session_console_app_token()
    if not app_token or not table_id:
        return {}
    try:
        return feishu_post_json(
            f"/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables/{urllib.parse.quote(table_id)}/records",
            {"fields": fields},
        )
    finally:
        invalidate_feishu_bitable_list_cache(table_id)


def update_feishu_bitable_record(table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    app_token = feishu_session_console_app_token()
    if not app_token or not table_id or not record_id:
        return {}
    try:
        return feishu_post_json(
            (
                f"/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables/"
                f"{urllib.parse.quote(table_id)}/records/{urllib.parse.quote(record_id)}"
            ),
            {"fields": fields},
            method="PUT",
        )
    finally:
        invalidate_feishu_bitable_list_cache(table_id)


# Every bitable lookup is a full paginated table scan filtered in memory, and one
# inbound message runs several of them back to back (session record, peer project
# config, chat mode) — twice over, because request_details is computed again inside
# build_webdock_body. Measured 2026-07-28: those serial scans were 2-3s of a group
# message's pre-work and 6-9s of a private one, i.e. the single largest remaining
# segment between a Feishu message and ChatGPT starting to type.
#
# So bitable is no longer read on the message path at all. A background thread keeps
# a snapshot of every table this process has touched, refreshed on a short cycle;
# reads serve from that snapshot. The TTL below is therefore not the freshness
# target any more — the refresh cycle is — it is only the age at which a snapshot is
# considered too stale to trust (refresher wedged/dead), where we fall back to a
# synchronous scan rather than answer from something ancient.
FEISHU_BITABLE_LIST_CACHE_SECONDS = float(os.getenv("FEISHU_BITABLE_LIST_CACHE_SECONDS", "900"))
# 60s, not tighter: this is the ONE knob that trades Feishu API volume for how fast
# a table edit applies. Polling N tables every T seconds costs 86400/T * N requests
# a day flat, whether or not anything changed — at 60s / 4 tables that is ~4
# requests a minute, far under any tenant limit, and the operator tolerance for an
# edit taking effect is a minute. Halving T doubles the bill for no latency win on
# the message path, which reads memory either way.
FEISHU_BITABLE_SNAPSHOT_REFRESH_SECONDS = float(
    os.getenv("FEISHU_BITABLE_SNAPSHOT_REFRESH_SECONDS", "60")
)
# Surviving a container restart matters because a cutover is exactly when the first
# message would otherwise pay the full cold scan again.
FEISHU_BITABLE_SNAPSHOT_PATH = os.getenv(
    "FEISHU_BITABLE_SNAPSHOT_PATH", "/var/lib/openclaw-bridge/bitable-snapshot.json"
)
_feishu_bitable_list_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_feishu_bitable_list_cache_lock = Lock()
# Which (app_token, table_id) pairs the refresher should keep warm. Seeded from the
# configured console tables at startup so even the very first message is warm, and
# extended by any table an actual lookup asks for.
_feishu_bitable_tracked_tables: set[str] = set()


def _bitable_cache_key(app_token: str, table_id: str) -> str:
    return f"{app_token}\t{table_id}"


def invalidate_feishu_bitable_list_cache(table_id: str = "") -> None:
    """Drop cached scans. Called after every write so a record this bridge just
    created or updated is never read back stale, and by the admin invalidate
    endpoint so an external push (bitable automation / event subscription) can make
    a manual edit apply immediately instead of waiting out the refresh cycle."""
    with _feishu_bitable_list_cache_lock:
        if not table_id:
            _feishu_bitable_list_cache.clear()
            return
        for key in [k for k in _feishu_bitable_list_cache if k.endswith("\t" + table_id)]:
            _feishu_bitable_list_cache.pop(key, None)


def _fetch_feishu_bitable_records(table_id: str, app_token: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_token = ""
    seen_tokens: set[str] = set()
    for _ in range(20):
        params = {"page_size": "500"}
        if page_token:
            params["page_token"] = page_token
        query = urllib.parse.urlencode(params)
        data = feishu_get_json(
            (
                f"/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables/"
                f"{urllib.parse.quote(table_id)}/records?{query}"
            )
        )
        block = data.get("data") or {}
        records.extend(block.get("items") or [])
        if not block.get("has_more"):
            break
        next_token = str(block.get("page_token") or "")
        if not next_token or next_token in seen_tokens:
            break
        seen_tokens.add(next_token)
        page_token = next_token
    return records


def _store_feishu_bitable_records(cache_key: str, records: list[dict[str, Any]]) -> None:
    with _feishu_bitable_list_cache_lock:
        _feishu_bitable_list_cache[cache_key] = (time.monotonic(), list(records))


def list_feishu_bitable_records(table_id: str, app_token: str | None = None) -> list[dict[str, Any]]:
    """Read a table. Normally answered from the background snapshot in ~0ms; the
    synchronous scan below only runs on a genuine cold miss (first touch of a table,
    or a snapshot so old the refresher must be broken)."""
    app_token = app_token or feishu_session_console_app_token()
    if not app_token or not table_id:
        return []
    cache_key = _bitable_cache_key(app_token, table_id)
    with _feishu_bitable_list_cache_lock:
        _feishu_bitable_tracked_tables.add(cache_key)
        cached = _feishu_bitable_list_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < FEISHU_BITABLE_LIST_CACHE_SECONDS:
            return list(cached[1])
    records = _fetch_feishu_bitable_records(table_id, app_token)
    _store_feishu_bitable_records(cache_key, records)
    return records


def feishu_bitable_tracked_tables() -> list[tuple[str, str]]:
    """(app_token, table_id) pairs the refresher keeps warm: every console table we
    know about from config, plus anything a lookup has actually asked for."""
    app_token = feishu_session_console_app_token()
    keys: set[str] = set()
    if app_token:
        for kind in ("session", "user", "group", "rule"):
            table_id = feishu_session_console_table_id(kind)
            if table_id:
                keys.add(_bitable_cache_key(app_token, table_id))
    with _feishu_bitable_list_cache_lock:
        keys.update(_feishu_bitable_tracked_tables)
    pairs = []
    for key in sorted(keys):
        token, _, table = key.partition("\t")
        if token and table:
            pairs.append((token, table))
    return pairs


def refresh_feishu_bitable_snapshot() -> tuple[int, list[str]]:
    """Re-scan every tracked table into the snapshot. Returns (ok_count, errors).

    Deliberately not transactional: one unreadable table must not stop the others
    from staying warm, and a failed table simply keeps its previous records until
    it either succeeds again or ages past FEISHU_BITABLE_LIST_CACHE_SECONDS, at
    which point reads fall back to a synchronous scan and surface the real error.
    """
    ok = 0
    errors: list[str] = []
    for app_token, table_id in feishu_bitable_tracked_tables():
        try:
            records = _fetch_feishu_bitable_records(table_id, app_token)
        except Exception as exc:
            errors.append(f"{table_id}: {exc}")
            continue
        _store_feishu_bitable_records(_bitable_cache_key(app_token, table_id), records)
        ok += 1
    return ok, errors


def feishu_bitable_snapshot_counts() -> dict[str, int]:
    """table_id -> record count, for the daily reconcile report."""
    with _feishu_bitable_list_cache_lock:
        return {
            key.partition("\t")[2]: len(entry[1])
            for key, entry in _feishu_bitable_list_cache.items()
        }


def save_feishu_bitable_snapshot() -> None:
    path = FEISHU_BITABLE_SNAPSHOT_PATH
    if not path:
        return
    with _feishu_bitable_list_cache_lock:
        # Stored against wall clock: monotonic is meaningless to the next process.
        payload = {
            "saved_at": time.time(),
            "tables": {key: entry[1] for key, entry in _feishu_bitable_list_cache.items()},
        }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        log_line(f"bitable_snapshot_save_failed {exc}")


def load_feishu_bitable_snapshot() -> int:
    """Warm the cache from disk at startup so the first message after a cutover
    does not pay the cold scan. A snapshot older than the staleness ceiling is
    ignored rather than served."""
    path = FEISHU_BITABLE_SNAPSHOT_PATH
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        log_line(f"bitable_snapshot_load_failed {exc}")
        return 0
    age = time.time() - float(payload.get("saved_at") or 0)
    if age < 0 or age > FEISHU_BITABLE_LIST_CACHE_SECONDS:
        log_line(f"bitable_snapshot_ignored stale_age_s={int(age)}")
        return 0
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        return 0
    # Age the loaded entries so they expire on the same schedule they would have.
    loaded_at = time.monotonic() - age
    with _feishu_bitable_list_cache_lock:
        for key, records in tables.items():
            if isinstance(key, str) and isinstance(records, list) and "\t" in key:
                _feishu_bitable_list_cache[key] = (loaded_at, records)
                _feishu_bitable_tracked_tables.add(key)
    return len(tables)


FEISHU_BITABLE_FIELD_TYPE_TEXT = 1
FEISHU_BITABLE_FIELD_TYPE_DATETIME = 5
FEISHU_BITABLE_FIELD_TYPE_CHECKBOX = 7
FEISHU_BITABLE_FIELD_TYPE_SINGLE_SELECT = 3
FEISHU_BITABLE_FIELD_TYPE_MULTI_SELECT = 4
FEISHU_BITABLE_FIELD_TYPE_URL = 15


def feishu_select_field_property(options: list[str]) -> dict[str, Any]:
    """单选/多选字段的 property：保序选项，color 轮转 0..15。"""
    return {"options": [{"name": name, "color": index % 16} for index, name in enumerate(options)]}


def list_feishu_bitable_fields(table_id: str, app_token: str | None = None) -> list[dict[str, Any]]:
    app_token = app_token or feishu_session_console_app_token()
    if not app_token or not table_id:
        return []
    data = feishu_get_json(
        f"/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables/{urllib.parse.quote(table_id)}/fields?page_size=100"
    )
    return (data.get("data") or {}).get("items") or []


def create_feishu_bitable_field(
    table_id: str,
    field_name: str,
    field_type: int = FEISHU_BITABLE_FIELD_TYPE_CHECKBOX,
    app_token: str | None = None,
    field_property: dict[str, Any] | None = None,
) -> dict[str, Any]:
    app_token = app_token or feishu_session_console_app_token()
    if not app_token or not table_id:
        return {}
    body: dict[str, Any] = {"field_name": field_name, "type": field_type}
    if field_property:
        body["property"] = field_property
    return feishu_post_json(
        f"/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables/{urllib.parse.quote(table_id)}/fields",
        body,
    )


def delete_feishu_bitable_field(table_id: str, field_id: str, app_token: str | None = None) -> dict[str, Any]:
    app_token = app_token or feishu_session_console_app_token()
    if not app_token or not table_id or not field_id:
        return {}
    return feishu_request_json(
        f"/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables/{urllib.parse.quote(table_id)}/fields/{urllib.parse.quote(field_id)}",
        method="DELETE",
    )


def ensure_feishu_bitable_fields(
    table_id: str,
    field_names: list[str],
    field_type: int = FEISHU_BITABLE_FIELD_TYPE_CHECKBOX,
    *,
    reconcile_type: bool = False,
    app_token: str | None = None,
    field_property: dict[str, Any] | None = None,
) -> None:
    """Best-effort: create any bitable columns that don't exist yet, so a later
    write to those field names doesn't fail with FieldNameNotFound (unlike
    WeCom smartsheets, Feishu Bitable requires a column to exist before a
    record write can set its value). Any failure (permission, API error, or
    being unable to list existing fields) is logged and skipped — never
    raises, matching the best-effort contract of its callers.

    ``reconcile_type=True`` additionally repairs a column that already exists
    but with the wrong type — a legacy Checkbox column where Text is now
    required makes every record write fail with CheckboxFieldConvFail. Only
    callers whose column values are bridge-managed/regenerable pass this, since
    the repair deletes and recreates the column (dropping its cell values)."""
    if not table_id or not field_names:
        return
    try:
        existing = {
            str(field.get("field_name") or ""): field
            for field in list_feishu_bitable_fields(table_id, app_token=app_token)
        }
    except Exception as exc:
        log_line(f"list_feishu_bitable_fields failed: {exc}")
        return
    for name in field_names:
        field = existing.get(name)
        if field is not None:
            if not reconcile_type or int(field.get("type") or 0) == field_type:
                continue
            try:
                delete_feishu_bitable_field(table_id, str(field.get("field_id") or ""), app_token=app_token)
            except Exception as exc:
                log_line(f"delete_feishu_bitable_field({name}) failed: {exc}")
                continue
        try:
            create_feishu_bitable_field(table_id, name, field_type, app_token=app_token, field_property=field_property)
        except Exception as exc:
            log_line(f"create_feishu_bitable_field({name}) failed: {exc}")


def bitable_url_value(url: str) -> dict[str, str]:
    return {"text": url, "link": url}


def bitable_url_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("link", "url", "text"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
    if isinstance(value, list) and value:
        return bitable_url_text(value[0])
    return bitable_field_text(value)


def bitable_link_value(*record_ids: str) -> list[str]:
    return [record_id for record_id in record_ids if record_id]


def bitable_created_record_id(result: dict[str, Any]) -> str:
    data = result.get("data") or {}
    record = data.get("record") or {}
    return str(record.get("record_id") or data.get("record_id") or "")


def _machine_like_feishu_name(value: str, peer_id: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if peer_id and text == peer_id:
        return True
    return text.startswith(("oc_", "ou_", "on_", "group-", "user-"))


def _merge_display_name(fields: dict[str, Any], field_name: str, candidate: str, peer_id: str) -> str:
    current = bitable_field_text(fields.get(field_name)).strip()
    if current and not _machine_like_feishu_name(current, peer_id):
        return current
    return str(candidate or current or peer_id)


def _ensure_peer_config_columns(table_id: str) -> None:
    try:
        ensure_feishu_bitable_fields(table_id, [CHATGPT_PROJECT_URL_FIELD], field_type=FEISHU_BITABLE_FIELD_TYPE_URL)
        ensure_feishu_bitable_fields(
            table_id,
            [CHATGPT_PROJECT_NAME_FIELD],
            field_type=FEISHU_BITABLE_FIELD_TYPE_TEXT,
        )
        ensure_feishu_bitable_fields(
            table_id,
            [FEISHU_NAME_RESOLVED_AT_FIELD],
            field_type=FEISHU_BITABLE_FIELD_TYPE_DATETIME,
        )
    except Exception as exc:
        log_line(f"ensure_peer_config_columns failed: {exc}")


def find_feishu_bitable_record(
    table_id: str,
    field_name: str,
    expected: str,
    app_token: str | None = None,
) -> dict[str, Any] | None:
    if not table_id or not expected:
        return None
    for record in list_feishu_bitable_records(table_id, app_token=app_token):
        fields = record.get("fields") or {}
        if isinstance(fields, dict) and bitable_field_text(fields.get(field_name)) == expected:
            return record
    return None


def upsert_feishu_user_record(details: dict[str, Any]) -> str:
    table_id = feishu_session_console_table_id("user")
    open_id = feishu_open_id(details)
    if not table_id or not open_id:
        return ""
    raw_metadata = details.get("raw_metadata") or {}
    now_ms = int(time.time() * 1000)
    _ensure_peer_config_columns(table_id)
    name = str(_first_metadata_value(raw_metadata, "name", "label", "sender_name") or open_id)
    fields = {
        "用户编号": f"user-{safe_bitable_id(open_id)}",
        "飞书用户名": name,
        "open_id": open_id,
        "union_id": str(_first_metadata_value(raw_metadata, "union_id", "unionId") or ""),
        "user_id": str(_first_metadata_value(raw_metadata, "user_id", "userId") or ""),
        "用户状态": "启用",
        "用户角色": "普通用户",
        "最近互动时间": now_ms,
        "最近名称解析时间": now_ms,
    }
    existing = find_feishu_bitable_record(table_id, "open_id", open_id)
    if existing:
        record_id = str(existing.get("record_id") or "")
        if record_id:
            current = existing.get("fields") or {}
            fields["飞书用户名"] = _merge_display_name(current, "飞书用户名", name, open_id)
            update_feishu_bitable_record(table_id, record_id, fields)
        return record_id
    return bitable_created_record_id(create_feishu_bitable_record(table_id, fields))


def upsert_feishu_group_record(details: dict[str, Any]) -> str:
    table_id = feishu_session_console_table_id("group")
    chat_id = feishu_chat_id(details)
    if not table_id or not chat_id:
        return ""
    raw_metadata = details.get("raw_metadata") or {}
    now_ms = int(time.time() * 1000)
    _ensure_peer_config_columns(table_id)
    name = str(_first_metadata_value(raw_metadata, "chat_name", "group_name", "room_name") or chat_id)
    fields: dict[str, Any] = {
        "群编号": f"group-{safe_bitable_id(chat_id)}",
        "群名称": name,
        "chat_id": chat_id,
        "最近消息时间": now_ms,
        "最近名称解析时间": now_ms,
    }
    if feishu_mentions_bot(details):
        fields["最近 @ 机器人时间"] = now_ms
    existing = find_feishu_bitable_record(table_id, "chat_id", chat_id)
    if existing:
        record_id = str(existing.get("record_id") or "")
        if record_id:
            current = existing.get("fields") or {}
            fields["群名称"] = _merge_display_name(current, "群名称", name, chat_id)
            update_feishu_bitable_record(table_id, record_id, fields)
        return record_id
    fields.update(
        {
            "群类型": "普通群",
            "是否启用机器人": True,
            "是否记录全量消息": True,
            "回复模式": "回复所有",
        }
    )
    return bitable_created_record_id(create_feishu_bitable_record(table_id, fields))


def ensure_feishu_default_rule_record() -> str:
    table_id = feishu_session_console_table_id("rule")
    if not table_id:
        return ""
    existing = find_feishu_bitable_record(table_id, "规则编号", "global-default")
    managed_defaults = _rule_managed_defaults()
    if existing:
        record_id = str(existing.get("record_id") or "")
        current = existing.get("fields") or {}
        missing = {k: v for k, v in managed_defaults.items() if k not in current}
        for key in (CHATGPT_MODE_DEFAULT_FIELD, CHATGPT_PROJECT_URL_FIELD, CHATGPT_PROJECT_NAME_FIELD):
            if key in current and not bitable_url_text(current.get(key)).strip():
                missing[key] = managed_defaults[key]
        if missing and record_id:
            try:
                _ensure_rule_columns(table_id, list(missing))
                update_feishu_bitable_record(table_id, record_id, missing)
            except Exception as exc:
                log_line(f"ensure_rule_record backfill failed: {exc}")
        return record_id
    _ensure_rule_columns(table_id, list(managed_defaults))
    fields = {
        "规则编号": "global-default",
        "规则名称": "默认飞书会话规则",
        "规则对象类型": "全局",
        "是否启用": True,
        "是否记录全量消息": True,
        "回复模式": "回复所有",
        "是否允许图片": True,
        "是否允许文件": True,
        "是否需要审核": False,
        "每日最大请求数": 0,
        "敏感群标记": False,
        "备注": "openclaw-bridge 自动维护",
        **managed_defaults,
    }
    return bitable_created_record_id(create_feishu_bitable_record(table_id, fields))


def append_feishu_session_console_records(details: dict[str, Any], reply: str, status: str) -> None:
    metadata = details.get("metadata") or {}
    if metadata.get("channel") != "feishu" or not feishu_session_console_configured():
        return
    try:
        should_send = feishu_should_send_chatgpt(details)
        user_record_id = upsert_feishu_user_record(details)
        group_record_id = upsert_feishu_group_record(details)
        session_record_id = ""
        if status == "已回复" and should_send:
            session_record_id = upsert_feishu_session_index_record(
                details,
                user_record_id=user_record_id,
                group_record_id=group_record_id,
            )
        message_fields = build_feishu_message_log_fields(details, reply=reply, status=status)
        if user_record_id:
            message_fields["关联用户记录"] = bitable_link_value(user_record_id)
        if group_record_id:
            message_fields["关联群记录"] = bitable_link_value(group_record_id)
        if session_record_id:
            message_fields["匹配会话记录"] = bitable_link_value(session_record_id)
        message_result = create_feishu_bitable_record(
            feishu_session_console_table_id("message"),
            message_fields,
        )
        message_record_id = bitable_created_record_id(message_result)
        task_table_id = feishu_session_console_table_id("task")
        if task_table_id and should_send:
            task_status = "已发送" if status == "已回复" else "失败"
            task_fields = build_feishu_reply_task_fields(details, reply=reply, status=task_status)
            if message_record_id:
                task_fields["关联消息记录"] = bitable_link_value(message_record_id)
            if session_record_id:
                task_fields["关联会话记录"] = bitable_link_value(session_record_id)
            create_feishu_bitable_record(
                task_table_id,
                task_fields,
            )
        if session_record_id and user_record_id and not feishu_is_group_message(details):
            update_feishu_bitable_record(
                feishu_session_console_table_id("user"),
                user_record_id,
                {"默认私聊会话记录": bitable_link_value(session_record_id)},
            )
        if session_record_id and group_record_id:
            update_feishu_bitable_record(
                feishu_session_console_table_id("group"),
                group_record_id,
                {"默认会话记录": bitable_link_value(session_record_id)},
            )
        ensure_feishu_default_rule_record()
    except Exception as exc:
        log_line(
            "feishu_bitable_write_failed "
            + json.dumps(
                {
                    "request_id": details.get("request_id"),
                    "message_id": metadata.get("message_id"),
                    "peer_id": metadata.get("peer_id"),
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )


def append_feishu_session_console_records_async(
    details: dict[str, Any], reply: str, status: str
) -> None:
    """Fire-and-forget wrapper for bitable session console writes.

    The sync version makes 5-8 bitable HTTP calls per request (upsert user/group/
    session + create message/task records + chase default-record links), adding
    2-3s to every chain_result, even on the short-circuit "仅记录" path that does
    not call WebDock at all. Detaching to a daemon thread frees the request
    handler immediately. Errors still surface via the sync function's print path,
    which is fine because bitable is a secondary audit log (the primary archive
    lives on the laptop in webdock /var/log/webdock/archive). details is
    deep-copied so the caller can mutate the original after firing.
    """
    Thread(
        target=append_feishu_session_console_records,
        args=(copy.deepcopy(details), reply, status),
        daemon=True,
        name="bitable-writer",
    ).start()


def build_feishu_message_log_fields(details: dict[str, Any], *, reply: str, status: str) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    metadata = details.get("metadata") or {}
    raw_metadata = details.get("raw_metadata") or {}
    text = str(details.get("user_text") or "")
    message_id = feishu_message_id(details)
    is_group = feishu_is_group_message(details)
    mentioned_bot = feishu_mentions_bot(details)
    should_send = feishu_should_send_chatgpt(details)
    command_type = feishu_command_type(text)
    fields: dict[str, Any] = {
        "日志编号": f"bridge-{message_id or details.get('request_id') or uuid.uuid4().hex[:8]}",
        "飞书 message_id": message_id,
        "event_id": str(_first_metadata_value(raw_metadata, "event_id", "eventId") or ""),
        "tenant_key": str(_first_metadata_value(raw_metadata, "tenant_key", "tenantKey") or ""),
        "消息时间": now_ms,
        "接收时间": now_ms,
        "聊天类型": "群聊" if is_group else "私聊",
        "关联用户": feishu_open_id(details),
        "关联群": feishu_chat_id(details),
        "发送人 open_id": feishu_open_id(details),
        "发送人名称": str(_first_metadata_value(raw_metadata, "name", "label", "sender_name") or ""),
        "群 chat_id": feishu_chat_id(details),
        "群名称": str(_first_metadata_value(raw_metadata, "chat_name", "group_name", "room_name") or ""),
        "消息类型": str(_first_metadata_value(raw_metadata, "message_type", "msg_type") or "text"),
        "原始消息内容": text,
        "清洗后内容": text,
        "是否 @ 机器人": mentioned_bot,
        "@对象列表": feishu_mentions_text(raw_metadata),
        "是否命令": command_type != "无",
        "命令类型": command_type,
        "是否需要送 ChatGPT": should_send,
        "不处理原因": "" if should_send else "未@机器人",
        "匹配会话": str(metadata.get("peer_id") or ""),
        "处理状态": status,
        "是否已回复飞书": status == "已回复",
        "飞书回复 message_id": "",
        "原始事件 JSON": json.dumps(raw_metadata or metadata, ensure_ascii=False, sort_keys=True, default=str),
        "错误信息": feishu_message_log_error(details),
    }
    attachment_url = str(_first_metadata_value(raw_metadata, "attachment_url", "file_url", "image_url") or "")
    if attachment_url:
        fields["附件链接"] = bitable_url_value(attachment_url)
    return fields


def build_feishu_reply_task_fields(details: dict[str, Any], *, reply: str, status: str) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    metadata = details.get("metadata") or {}
    message_id = feishu_message_id(details)
    fields: dict[str, Any] = {
        "任务编号": f"task-{message_id or details.get('request_id') or uuid.uuid4().hex[:8]}",
        "关联消息": message_id,
        "关联会话": str(metadata.get("peer_id") or ""),
        "任务类型": feishu_task_type(str(details.get("user_text") or "")),
        "任务状态": status,
        "给 ChatGPT 的输入": str(details.get("user_text") or ""),
        "ChatGPT 回复内容": reply,
        "是否需要人工审核": False,
        "审核状态": "无需审核",
        "处理人": "",
        "处理开始时间": now_ms,
        "处理完成时间": now_ms,
        "发送结果": "已回复飞书" if status == "已发送" else status,
        "失败原因": "" if status == "已发送" else reply,
        "备注": "",
    }
    chatgpt_url = str(_first_metadata_value(metadata, "chatgpt_conversation_url", "chatgpt_url") or "")
    if chatgpt_url:
        fields["ChatGPT 对话链接"] = bitable_url_value(chatgpt_url)
    return fields


def upsert_feishu_session_index_record(
    details: dict[str, Any],
    *,
    user_record_id: str = "",
    group_record_id: str = "",
) -> str:
    if not feishu_session_index_configured():
        return ""
    metadata = details.get("metadata") or {}
    conversation_url = str(
        _first_metadata_value(metadata, "chatgpt_conversation_url", "chatgpt_url", "chatgpt_conversation")
        or ""
    ).strip()
    if not conversation_url:
        return ""
    session_key = feishu_session_key(details)
    if not session_key:
        return ""
    table_id = feishu_session_console_table_id("session")
    records = find_feishu_session_records(session_key)
    same_current = None
    current_records: list[dict[str, Any]] = []
    max_version = 0
    max_message_count = 0
    max_mention_count = 0
    for record in records:
        fields = record.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        max_version = max(max_version, bitable_field_int(fields.get("会话版本")))
        max_message_count = max(max_message_count, bitable_field_int(fields.get("消息数量")))
        max_mention_count = max(max_mention_count, bitable_field_int(fields.get("@机器人次数")))
        if bitable_truthy(fields.get("是否当前会话")) and bitable_field_text(fields.get("会话状态")) in {"活跃", "待创建"}:
            current_records.append(record)
            if bitable_field_text(fields.get("ChatGPT 对话链接")) == conversation_url:
                same_current = record

    if same_current:
        fields = same_current.get("fields") or {}
        update_feishu_bitable_record(
            table_id,
            str(same_current.get("record_id") or ""),
            build_feishu_session_index_fields(
                details,
                session_key=session_key,
                version=max(1, bitable_field_int(fields.get("会话版本"))),
                message_count=bitable_field_int(fields.get("消息数量")) + 1,
                mention_count=bitable_field_int(fields.get("@机器人次数")) + (1 if feishu_mentions_bot(details) else 0),
                user_record_id=user_record_id,
                group_record_id=group_record_id,
            ),
        )
        return str(same_current.get("record_id") or "")

    for record in current_records:
        record_id = str(record.get("record_id") or "")
        if record_id:
            update_feishu_bitable_record(table_id, record_id, {"会话状态": "已归档", "是否当前会话": False})

    created = create_feishu_bitable_record(
        table_id,
        build_feishu_session_index_fields(
            details,
            session_key=session_key,
            version=max_version + 1 if max_version else 1,
            message_count=max_message_count + 1,
            mention_count=max_mention_count + (1 if feishu_mentions_bot(details) else 0),
            user_record_id=user_record_id,
            group_record_id=group_record_id,
        ),
    )
    return bitable_created_record_id(created)


def build_feishu_session_index_fields(
    details: dict[str, Any],
    *,
    session_key: str,
    version: int,
    message_count: int,
    mention_count: int,
    user_record_id: str = "",
    group_record_id: str = "",
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    metadata = details.get("metadata") or {}
    raw_metadata = details.get("raw_metadata") or {}
    conversation_url = str(_first_metadata_value(metadata, "chatgpt_conversation_url", "chatgpt_url") or "")
    configured_project_url, configured_project_name = feishu_chatgpt_project_config(details)
    project_url = (
        str(_first_metadata_value(metadata, "chatgpt_project_url") or "")
        or configured_project_url
        or project_home_from_conversation_url(conversation_url)
    )
    session_type = feishu_session_type(details)
    name = feishu_session_display_name(details)
    fields: dict[str, Any] = {
        "会话编号": f"{safe_bitable_id(session_key)}-v{version}",
        "会话名称": name,
        "会话类型": session_type,
        "session_key": session_key,
        "关联用户": feishu_open_id(details),
        "关联群": feishu_chat_id(details),
        "飞书用户名": str(_first_metadata_value(raw_metadata, "name", "label", "sender_name") or ""),
        "飞书群名": str(_first_metadata_value(raw_metadata, "chat_name", "group_name", "room_name") or ""),
        "ChatGPT 项目名": str(
            _first_metadata_value(metadata, "chatgpt_project")
            or configured_project_name
            or feishu_default_chatgpt_project_name()
        ),
        "ChatGPT 对话标题": name,
        "会话状态": "活跃",
        "是否当前会话": True,
        "会话版本": max(1, version),
        "上下文摘要": "",
        "系统提示词 / 角色设定": "",
        "回复风格": "",
        "最近活跃时间": now_ms,
        "消息数量": max(1, message_count),
        "@机器人次数": max(0, mention_count),
        "备注": f"message_id={feishu_message_id(details)}",
    }
    if version <= 1:
        fields["创建时间"] = now_ms
    if project_url:
        fields["ChatGPT 项目首页链接"] = bitable_url_value(project_url)
    if conversation_url:
        fields["ChatGPT 对话链接"] = bitable_url_value(conversation_url)
    if user_record_id:
        fields["关联用户记录"] = bitable_link_value(user_record_id)
    if group_record_id:
        fields["关联群记录"] = bitable_link_value(group_record_id)
    return fields


def find_current_feishu_session_record(session_key: str) -> dict[str, Any] | None:
    for record in find_feishu_session_records(session_key):
        fields = record.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        if bitable_field_text(fields.get("会话状态")) not in {"活跃", "待创建"}:
            continue
        if not bitable_truthy(fields.get("是否当前会话")):
            continue
        return record
    return None


def find_feishu_session_records(session_key: str) -> list[dict[str, Any]]:
    if not session_key or not feishu_session_index_configured():
        return []
    table_id = feishu_session_console_table_id("session")
    matched = []
    for record in list_feishu_bitable_records(table_id):
        fields = record.get("fields") or {}
        if isinstance(fields, dict) and bitable_field_text(fields.get("session_key")) == session_key:
            matched.append(record)
    return matched


def feishu_session_key(details: dict[str, Any]) -> str:
    return feishu_session_key_from_metadata(details.get("metadata") or {}, details.get("raw_metadata") or {})


def feishu_session_key_from_metadata(metadata: dict[str, Any], raw_metadata: dict[str, Any]) -> str:
    tenant_key = str(
        _first_metadata_value(raw_metadata, "tenant_key", "tenantKey")
        or _first_metadata_value(metadata, "tenant_key", "tenantKey")
        or os.getenv("FEISHU_TENANT_KEY")
        or "default"
    ).strip()
    peer_id = str(metadata.get("peer_id") or "").strip()
    if peer_id.startswith("group_user:"):
        parts = peer_id.split(":", 2)
        if len(parts) == 3:
            return f"{tenant_key}:group_user:{_strip_lane_peer_prefix(parts[1])}:{_strip_lane_peer_prefix(parts[2])}"
    if peer_id.startswith("group:"):
        return f"{tenant_key}:group:{_strip_lane_peer_prefix(peer_id)}"
    if peer_id.startswith("user:"):
        return f"{tenant_key}:user:{_strip_lane_peer_prefix(peer_id)}"
    chat_id = _first_metadata_value(raw_metadata, "chat_id", "chatId", "conversation_id", "room_id")
    if chat_id:
        return f"{tenant_key}:group:{_strip_lane_peer_prefix(chat_id)}"
    open_id = _first_metadata_value(raw_metadata, "open_id", "openId", "sender_id", "user_id", "from_user_id")
    if open_id:
        return f"{tenant_key}:user:{_strip_lane_peer_prefix(open_id)}"
    return ""


def feishu_session_type(details: dict[str, Any]) -> str:
    peer_id = str((details.get("metadata") or {}).get("peer_id") or "")
    if peer_id.startswith("group_user:"):
        return "群内个人"
    return "群聊" if feishu_is_group_message(details) else "私聊"


def feishu_session_display_name(details: dict[str, Any]) -> str:
    raw_metadata = details.get("raw_metadata") or {}
    if feishu_is_group_message(details):
        return str(
            _first_metadata_value(raw_metadata, "chat_name", "group_name", "room_name")
            or feishu_chat_id(details)
            or "飞书群聊"
        )
    return str(
        _first_metadata_value(raw_metadata, "name", "label", "sender_name")
        or feishu_open_id(details)
        or "飞书私聊"
    )


def bitable_field_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    if isinstance(value, list):
        return "".join(bitable_field_text(item) for item in value).strip()
    if isinstance(value, dict):
        for key in ("text", "link", "url", "name", "value"):
            text = bitable_field_text(value.get(key))
            if text:
                return text
    return str(value).strip()


def bitable_field_int(value: Any) -> int:
    try:
        return int(float(bitable_field_text(value) or 0))
    except (TypeError, ValueError):
        return 0


def bitable_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = bitable_field_text(value).lower()
    return text in {"true", "1", "yes", "y", "on", "是", "当前", "活跃", "✓"}


def project_home_from_conversation_url(url: str) -> str:
    text = str(url or "").strip()
    marker = "/c/"
    if "chatgpt.com/g/" in text and marker in text:
        return text.split(marker, 1)[0].rstrip("/") + "/project"
    return ""


def safe_bitable_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", value).strip("_")[:120] or uuid.uuid4().hex[:8]


def feishu_message_id(details: dict[str, Any]) -> str:
    metadata = details.get("metadata") or {}
    raw_metadata = details.get("raw_metadata") or {}
    return str(_first_metadata_value(metadata, "message_id") or _first_metadata_value(raw_metadata, "message_id") or "")


def feishu_open_id(details: dict[str, Any]) -> str:
    metadata = details.get("metadata") or {}
    raw_metadata = details.get("raw_metadata") or {}
    value = _first_metadata_value(raw_metadata, "open_id", "openId", "sender_id", "user_id", "from_user_id")
    if value:
        return _strip_lane_peer_prefix(value)
    peer_id = str(metadata.get("peer_id") or "")
    if peer_id.startswith("user:"):
        return _strip_lane_peer_prefix(peer_id)
    return ""


def feishu_chat_id(details: dict[str, Any]) -> str:
    metadata = details.get("metadata") or {}
    raw_metadata = details.get("raw_metadata") or {}
    peer_id = str(metadata.get("peer_id") or "")
    chat_type = metadata.get("chat_type") or raw_metadata.get("chat_type")
    if not peer_id.startswith("group:") and not _is_group_chat(chat_type):
        return ""
    value = _first_metadata_value(raw_metadata, "chat_id", "chatId", "conversation_id", "room_id")
    if value:
        return _strip_lane_peer_prefix(value)
    if peer_id.startswith("group:"):
        return _strip_lane_peer_prefix(peer_id)
    return ""


def feishu_is_group_message(details: dict[str, Any]) -> bool:
    metadata = details.get("metadata") or {}
    raw_metadata = details.get("raw_metadata") or {}
    if str(metadata.get("peer_id") or "").startswith("group:"):
        return True
    if _is_group_chat(metadata.get("chat_type") or raw_metadata.get("chat_type")):
        return True
    text = str(details.get("user_text") or "")
    return feishu_has_group_mention_hint(text) and not feishu_chat_id(details)


def feishu_has_group_mention_hint(text: str) -> bool:
    lowered = text.lower()
    return "content may include mention tags" in lowered or "that mention refers to you" in lowered


def feishu_mentions_bot(details: dict[str, Any]) -> bool:
    raw_metadata = details.get("raw_metadata") or {}
    metadata = details.get("metadata") or {}
    for key in ("mentionedBot", "mentioned_bot", "is_mentioned", "isMentioned", "wasMentioned", "was_mentioned"):
        value = _first_metadata_value(raw_metadata, key) or _first_metadata_value(metadata, key)
        if isinstance(value, bool):
            return value
        if str(value or "").strip().lower() in {"1", "true", "yes"}:
            return True
    mentions = raw_metadata.get("mentions")
    if isinstance(mentions, list) and mentions:
        return True
    text = str(details.get("user_text") or "")
    return feishu_has_group_mention_hint(text) or "<at " in text.lower()


def feishu_should_send_chatgpt(details: dict[str, Any]) -> bool:
    if not feishu_is_group_message(details):
        return True
    enabled, reply_mode = feishu_group_reply_policy(details)
    if not enabled:
        return False
    if reply_mode == "仅@回复":
        return feishu_mentions_bot(details)
    return True


def feishu_group_reply_policy(details: dict[str, Any]) -> tuple[bool, str]:
    """Return the manually controlled group policy, defaulting safely to reply-all.

    Bitable is a runtime control plane, so cache briefly to avoid multiple API
    scans during one request while still applying manual changes quickly.
    """
    chat_id = feishu_chat_id(details)
    if not chat_id:
        return True, "回复所有"
    now = time.monotonic()
    with _feishu_group_policy_cache_lock:
        cached = _feishu_group_policy_cache.get(chat_id)
        if cached and now - cached[0] < FEISHU_GROUP_POLICY_CACHE_SECONDS:
            return cached[1], cached[2]

    enabled = True
    reply_mode = "回复所有"
    table_id = feishu_session_console_table_id("group")
    if table_id:
        try:
            record = find_feishu_bitable_record(table_id, "chat_id", chat_id)
            fields = (record or {}).get("fields") or {}
            if "是否启用机器人" in fields:
                enabled = bitable_truthy(fields.get("是否启用机器人"))
            configured_mode = bitable_field_text(fields.get("回复模式")).strip()
            if configured_mode in {"回复所有", "仅@回复"}:
                reply_mode = configured_mode
        except Exception as exc:
            log_line(
                "feishu_group_policy_read_failed "
                + json.dumps({"chat_id": chat_id, "error": str(exc)}, ensure_ascii=False, sort_keys=True)
            )

    with _feishu_group_policy_cache_lock:
        _feishu_group_policy_cache[chat_id] = (now, enabled, reply_mode)
    return enabled, reply_mode


CHATGPT_MODE_LABELS = {"极速": "fast", "均衡": "balanced", "高级": "advanced"}
CHATGPT_MODE_NAMES = {value: key for key, value in CHATGPT_MODE_LABELS.items()}
CHATGPT_MODE_FIELD = "对话模式"
CHATGPT_MODE_DEFAULT_FIELD = "对话模式默认"
CHATGPT_MODE_DEFAULT_RECORD_ID_FIELD = "配置编号"
CHATGPT_MODE_DEFAULT_RECORD_ID = "global-default"
CHATGPT_MODE_DEFAULT_FALLBACK = "advanced"
CHATGPT_PROJECT_URL_FIELD = "默认新对话项目链接"
CHATGPT_PROJECT_NAME_FIELD = "默认新对话项目名称"
FEISHU_NAME_RESOLVED_AT_FIELD = "最近名称解析时间"
FEISHU_CHAT_MODE_CACHE_SECONDS = float(os.getenv("FEISHU_CHAT_MODE_CACHE_SECONDS", "30"))
_feishu_chat_mode_cache: dict[str, tuple[float, str]] = {}
_feishu_chat_mode_cache_lock = Lock()


def parse_feishu_mode_command(text: str) -> tuple[bool, str]:
    """(是否 /模式 命令, 规范模式值)。参数缺失/非法时规范值为 ""（回用法提示）。"""
    stripped = (text or "").strip()
    if not stripped.startswith("/模式"):
        return False, ""
    argument = stripped[len("/模式"):].strip()
    return True, CHATGPT_MODE_LABELS.get(argument, "")


def feishu_mode_peer(details: dict[str, Any]) -> tuple[str, str]:
    """模式状态的归属：群聊挂群表(chat_id)，私聊挂用户表(open_id)。"""
    if feishu_is_group_message(details):
        return "group", feishu_chat_id(details)
    return "user", feishu_open_id(details)


def feishu_chat_mode_default() -> str:
    try:
        label = str(feishu_global_rule_policy().get(CHATGPT_MODE_DEFAULT_FIELD) or "").strip()
        return CHATGPT_MODE_LABELS.get(label) or CHATGPT_MODE_DEFAULT_FALLBACK
    except Exception as exc:
        log_line(
            "feishu_chat_mode_default_read_failed "
            + json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True)
        )
        return CHATGPT_MODE_DEFAULT_FALLBACK


def feishu_global_chatgpt_project_config() -> tuple[str, str]:
    try:
        policy = feishu_global_rule_policy()
    except Exception:  # noqa: BLE001 - 全局项目配置不可读时仍回退 env
        policy = {}
    project_url = bitable_url_text(policy.get(CHATGPT_PROJECT_URL_FIELD)).strip()
    project_name = bitable_field_text(policy.get(CHATGPT_PROJECT_NAME_FIELD)).strip()
    return project_url or feishu_default_chatgpt_project_url(), project_name or feishu_env_chatgpt_project_name()


def feishu_peer_chatgpt_project_config(details: dict[str, Any]) -> tuple[str, str]:
    kind, key = feishu_mode_peer(details)
    if not key:
        return "", ""
    table_id = feishu_session_console_table_id(kind)
    if not table_id:
        return "", ""
    try:
        record = find_feishu_bitable_record(table_id, "chat_id" if kind == "group" else "open_id", key)
        fields = (record or {}).get("fields") or {}
        return (
            bitable_url_text(fields.get(CHATGPT_PROJECT_URL_FIELD)).strip(),
            bitable_field_text(fields.get(CHATGPT_PROJECT_NAME_FIELD)).strip(),
        )
    except Exception as exc:
        log_line(
            "feishu_project_config_read_failed "
            + json.dumps({"peer": f"{kind}:{key}", "error": str(exc)}, ensure_ascii=False, sort_keys=True)
        )
        return "", ""


def feishu_chatgpt_project_config(details: dict[str, Any]) -> tuple[str, str]:
    project_url, project_name = feishu_global_chatgpt_project_config()
    peer_url, peer_name = feishu_peer_chatgpt_project_config(details)
    return peer_url or project_url, peer_name or project_name


def feishu_chat_mode(details: dict[str, Any]) -> str:
    """当前会话模式（规范值）：粘性模式优先，未设置时回退系统默认。

    bitable 是事实源；读失败时保留内存里的旧值（退化为纯内存，符合
    "bitable 不可用只降级不阻断"的约定）。"""
    kind, key = feishu_mode_peer(details)
    if not key:
        return ""
    cache_key = f"{kind}:{key}"
    now = time.monotonic()
    with _feishu_chat_mode_cache_lock:
        cached = _feishu_chat_mode_cache.get(cache_key)
        if cached and now - cached[0] < FEISHU_CHAT_MODE_CACHE_SECONDS:
            return cached[1]
    mode = cached[1] if cached else ""
    table_id = feishu_session_console_table_id(kind)
    if table_id:
        try:
            record = find_feishu_bitable_record(
                table_id, "chat_id" if kind == "group" else "open_id", key
            )
            label = bitable_field_text(((record or {}).get("fields") or {}).get(CHATGPT_MODE_FIELD)).strip()
            mode = CHATGPT_MODE_LABELS.get(label, "")
        except Exception as exc:
            log_line(
                "feishu_chat_mode_read_failed "
                + json.dumps({"peer": cache_key, "error": str(exc)}, ensure_ascii=False, sort_keys=True)
            )
    if not mode:
        mode = feishu_chat_mode_default()
    with _feishu_chat_mode_cache_lock:
        _feishu_chat_mode_cache[cache_key] = (now, mode)
    return mode


def set_feishu_chat_mode(details: dict[str, Any], mode: str) -> bool:
    """设置会话粘性模式。内存立即生效；bitable 持久化 best-effort。"""
    kind, key = feishu_mode_peer(details)
    if not key:
        return False
    with _feishu_chat_mode_cache_lock:
        _feishu_chat_mode_cache[f"{kind}:{key}"] = (time.monotonic(), mode)
    table_id = feishu_session_console_table_id(kind)
    if not table_id:
        return True
    try:
        ensure_feishu_bitable_fields(
            table_id,
            [CHATGPT_MODE_FIELD],
            field_type=FEISHU_BITABLE_FIELD_TYPE_TEXT,
            reconcile_type=True,
        )
        record_id = (
            upsert_feishu_group_record(details) if kind == "group" else upsert_feishu_user_record(details)
        )
        if record_id:
            update_feishu_bitable_record(table_id, record_id, {CHATGPT_MODE_FIELD: CHATGPT_MODE_NAMES[mode]})
    except Exception as exc:
        log_line(
            "feishu_chat_mode_persist_failed "
            + json.dumps(
                {"peer": f"{kind}:{key}", "mode": mode, "error": str(exc)}, ensure_ascii=False, sort_keys=True
            )
        )
    return True


def feishu_mentions_text(raw_metadata: dict[str, Any]) -> str:
    mentions = raw_metadata.get("mentions")
    if mentions is None:
        return ""
    return json.dumps(mentions, ensure_ascii=False, sort_keys=True, default=str)


def feishu_command_type(text: str) -> str:
    stripped = text.strip()
    for command in ("/新对话", "/重置", "/摘要"):
        if stripped.startswith(command):
            return command
    return "无"


def feishu_task_type(text: str) -> str:
    command = feishu_command_type(text)
    if command == "/新对话":
        return "新建会话"
    if command == "/重置":
        return "重置会话"
    if command == "/摘要":
        return "总结会话"
    return "普通回复"


def feishu_message_log_error(details: dict[str, Any]) -> str:
    if feishu_has_group_mention_hint(str(details.get("user_text") or "")) and not feishu_chat_id(details):
        return "疑似飞书群聊 @ 机器人消息，但 OpenClaw 请求缺少 chat_id/oc_，bridge 暂无法绑定群会话"
    return ""


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
        "ts": utc_now_iso(),
        "event": event,
        "request_id": details.get("request_id"),
        "channel": metadata.get("channel") or "wechat",
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


def chain_result_kind(
    reply: str | None,
    *,
    http_code: int | None = None,
    error: BaseException | None = None,
) -> str:
    """Classify the WebDock/ChatGPT round-trip outcome for chain_result tracing."""
    if http_code is not None:
        return f"http_{http_code}"
    if isinstance(error, TimeoutError):
        return "timeout"
    if error is not None:
        return "unreachable"
    if not reply:
        return "empty"
    if reply.startswith(FALLBACK_MESSAGE):
        return "fallback"
    return "ok"


def trace_chain_result(
    details: dict[str, Any],
    started: float,
    *,
    reply: str | None = None,
    http_code: int | None = None,
    error: BaseException | None = None,
    webdock_call_ms: int | None = None,
) -> None:
    """Emit the WebDock/ChatGPT round-trip result so chain-logger can show 'did the
    downstream answer, how slow, and where it broke' — the scope-B hop that was
    missing from the timeline. Channel-tagged so Feishu and WeChat are both visible."""
    if not trace_enabled():
        return
    metadata = details.get("metadata") or {}
    payload = {
        "ts": utc_now_iso(),
        "event": "chain_result",
        "request_id": details.get("request_id"),
        "channel": metadata.get("channel") or "wechat",
        "wechat_account": metadata.get("wechat_account"),
        "chat_type": metadata.get("chat_type"),
        "peer_id": metadata.get("peer_id"),
        "message_id": metadata.get("message_id"),
        "result": chain_result_kind(reply, http_code=http_code, error=error),
        # NOTE: webdock_ms is the whole build_reply span (group policy, batching,
        # placeholder card, WebDock call, media/card delivery) — NOT the WebDock
        # hop, despite the name. Kept for chain-logger compatibility; the actual
        # HTTP round trip is webdock_call_ms.
        "webdock_ms": int((time.monotonic() - started) * 1000),
        "reply_len": len(reply or ""),
    }
    if webdock_call_ms is not None:
        payload["webdock_call_ms"] = webdock_call_ms
    print("bridge_request_trace " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def trace_stage(details: dict[str, Any], event: str, started: float, **fields: Any) -> None:
    """Timestamp a boundary inside build_reply. Without these, everything between
    batch_flush and chain_result was one opaque block and the 2026-07-28 latency
    report could not be attributed to the bridge or to WebDock."""
    if not trace_enabled():
        return
    metadata = details.get("metadata") or {}
    payload = {
        "ts": utc_now_iso(),
        "event": event,
        "request_id": details.get("request_id"),
        "channel": metadata.get("channel") or "wechat",
        "peer_id": metadata.get("peer_id"),
        "message_id": metadata.get("message_id"),
        "since_start_ms": int((time.monotonic() - started) * 1000),
    }
    payload.update(fields)
    print("bridge_request_trace " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


# --- 飞书"处理中"单卡片：在飞计数器 + 配置/文案 + 占位卡发送器 ---------------

_inflight_counts: dict[str, int] = {}
_inflight_lock = Lock()
_feishu_global_rule_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_feishu_global_rule_cache_lock = Lock()

DEFAULT_PROCESSING_ACK_TEXT = "📨 已投递到 ChatGPT，正在生成（约 20–60 秒）。答案会直接更新到这张卡片，请勿重复提问 🙏"
DEFAULT_PROCESSING_REMIND_TEXT = "⚠️ 上一条还在 ChatGPT 处理中，这条已排队。请等上面那张卡片出结果再问，连续提问会拖慢每一条。"
DEFAULT_PROCESSING_EMPTY_TEXT = "本次没有生成内容，请稍后重试。"
DEFAULT_DONE_MARKER = "🌿 回复完毕"
# 占位卡轮播提示：一行一条，与基础占位文案轮换显示。
DEFAULT_PLACEHOLDER_TIPS = (
    "💡 提示：/新对话 开启新会话，/模式 极速｜均衡｜高级 切换速度\n"
    "🔀 刚用 /模式 切换过？设置在下一次提问时才到页面生效，首次约多等 20 秒\n"
    "⏳ 长回复生成中，请勿重复提问，答案会更新到这张卡片"
)

# 规则表 global-default 行上的文案列：列名 → (env 覆盖变量, 代码默认)。表格非空值 > env > 默认。
RULE_TEXT_FIELDS: dict[str, tuple[str, str]] = {
    "处理中文案": ("OPENCLAW_BRIDGE_PROCESSING_ACK_TEXT", DEFAULT_PROCESSING_ACK_TEXT),
    "追问文案": ("OPENCLAW_BRIDGE_PROCESSING_REMIND_TEXT", DEFAULT_PROCESSING_REMIND_TEXT),
    "空回复文案": ("OPENCLAW_BRIDGE_PROCESSING_EMPTY_TEXT", DEFAULT_PROCESSING_EMPTY_TEXT),
    "完成标记": ("OPENCLAW_BRIDGE_DONE_MARKER", DEFAULT_DONE_MARKER),
    "轮播提示文案": ("OPENCLAW_BRIDGE_PLACEHOLDER_TIPS", DEFAULT_PLACEHOLDER_TIPS),
}


def _rule_text_env_defaults() -> dict[str, str]:
    return {key: os.getenv(env_name, default) for key, (env_name, default) in RULE_TEXT_FIELDS.items()}


def _rule_managed_defaults() -> dict[str, Any]:
    project_url = feishu_default_chatgpt_project_url()
    return {
        "处理中卡片": True,
        "调试尾注": True,
        "占位轮播": True,
        **_rule_text_env_defaults(),
        CHATGPT_MODE_DEFAULT_FIELD: CHATGPT_MODE_NAMES[CHATGPT_MODE_DEFAULT_FALLBACK],
        CHATGPT_PROJECT_URL_FIELD: bitable_url_value(project_url) if project_url else "",
        CHATGPT_PROJECT_NAME_FIELD: feishu_default_chatgpt_project_name(),
    }


def _ensure_rule_columns(table_id: str, names: list[str]) -> None:
    """规则表建列：布尔开关列建 Checkbox(默认)，文案列建文本(type=1)。
    文本文案若误建进 Checkbox 列，写入时会 CheckboxFieldConvFail 而静默丢失。"""
    text_field_names = set(RULE_TEXT_FIELDS)
    url_names = [name for name in names if name == CHATGPT_PROJECT_URL_FIELD]
    mode_names = [name for name in names if name == CHATGPT_MODE_DEFAULT_FIELD]
    text_names = [name for name in names if name in text_field_names]
    project_name_names = [name for name in names if name == CHATGPT_PROJECT_NAME_FIELD]
    special = text_field_names | {CHATGPT_PROJECT_URL_FIELD, CHATGPT_MODE_DEFAULT_FIELD, CHATGPT_PROJECT_NAME_FIELD}
    bool_names = [name for name in names if name not in special]
    if bool_names:
        ensure_feishu_bitable_fields(table_id, bool_names)
    if text_names:
        ensure_feishu_bitable_fields(
            table_id, text_names, field_type=FEISHU_BITABLE_FIELD_TYPE_TEXT, reconcile_type=True
        )
    if project_name_names:
        ensure_feishu_bitable_fields(
            table_id, project_name_names, field_type=FEISHU_BITABLE_FIELD_TYPE_TEXT
        )
    if url_names:
        ensure_feishu_bitable_fields(table_id, url_names, field_type=FEISHU_BITABLE_FIELD_TYPE_URL)
    if mode_names:
        ensure_feishu_bitable_fields(
            table_id,
            mode_names,
            field_type=FEISHU_BITABLE_FIELD_TYPE_SINGLE_SELECT,
            reconcile_type=True,
            field_property=feishu_select_field_property(list(CHATGPT_MODE_LABELS)),
        )


def _enter_inflight(lane_key: str) -> bool:
    """Register one in-flight webdock call for this lane. Returns True when another
    call was already in flight (i.e. this is a follow-up question)."""
    if not lane_key:
        return False
    with _inflight_lock:
        prior = _inflight_counts.get(lane_key, 0)
        _inflight_counts[lane_key] = prior + 1
        return prior > 0


def _exit_inflight(lane_key: str) -> None:
    """Deregister one in-flight call; drops the key at zero. Empty key is a no-op."""
    if not lane_key:
        return
    with _inflight_lock:
        n = _inflight_counts.get(lane_key, 1) - 1
        if n <= 0:
            _inflight_counts.pop(lane_key, None)
        else:
            _inflight_counts[lane_key] = n


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def feishu_global_rule_policy() -> dict[str, Any]:
    """Global feishu switches + reply texts, sourced from the rule bitable's
    ``global-default`` row with a short TTL cache. Any failure (no table / no creds /
    read error / missing field / empty text) falls back to env so a broken or absent
    table never changes behavior."""
    env_defaults: dict[str, Any] = {
        "处理中卡片": _env_flag("OPENCLAW_BRIDGE_PROCESSING_CARD", False),
        "调试尾注": _env_flag("OPENCLAW_BRIDGE_DEBUG_TRAILER", True),
        "占位轮播": _env_flag("OPENCLAW_BRIDGE_PLACEHOLDER_ROTATE", True),
        **_rule_text_env_defaults(),
        CHATGPT_MODE_DEFAULT_FIELD: CHATGPT_MODE_NAMES[CHATGPT_MODE_DEFAULT_FALLBACK],
        CHATGPT_PROJECT_URL_FIELD: feishu_default_chatgpt_project_url(),
    }
    now = time.monotonic()
    with _feishu_global_rule_cache_lock:
        cached = _feishu_global_rule_cache.get("value")
        if cached and now - cached[0] < FEISHU_GROUP_POLICY_CACHE_SECONDS:
            return dict(cached[1])
    result = dict(env_defaults)
    table_id = feishu_session_console_table_id("rule")
    if table_id:
        try:
            record = find_feishu_bitable_record(table_id, "规则编号", "global-default")
            fields = (record or {}).get("fields") or {}
            for key in ("处理中卡片", "调试尾注", "占位轮播"):
                if key in fields:
                    result[key] = bitable_truthy(fields.get(key))
            for key in RULE_TEXT_FIELDS:
                text = bitable_field_text(fields.get(key)) if key in fields else ""
                if text:
                    result[key] = text
            label = bitable_field_text(fields.get(CHATGPT_MODE_DEFAULT_FIELD)).strip()
            if label:
                result[CHATGPT_MODE_DEFAULT_FIELD] = label
            project_url = bitable_url_text(fields.get(CHATGPT_PROJECT_URL_FIELD)).strip()
            if project_url:
                result[CHATGPT_PROJECT_URL_FIELD] = project_url
            project_name = bitable_field_text(fields.get(CHATGPT_PROJECT_NAME_FIELD)).strip()
            if project_name:
                result[CHATGPT_PROJECT_NAME_FIELD] = project_name
        except Exception as exc:
            log_line(
                "feishu_global_rule_read_failed "
                + json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True)
            )
    with _feishu_global_rule_cache_lock:
        _feishu_global_rule_cache["value"] = (now, dict(result))
    return result


def invalidate_global_rule_cache() -> None:
    with _feishu_global_rule_cache_lock:
        _feishu_global_rule_cache.clear()


def processing_card_enabled() -> bool:
    return bool(feishu_global_rule_policy().get("处理中卡片"))


def debug_trailer_enabled() -> bool:
    return bool(feishu_global_rule_policy().get("调试尾注"))


def _rule_text(key: str) -> str:
    """文案取值：规则表非空值 >（policy 兜底已折入的）env > 代码默认。"""
    env_name, default = RULE_TEXT_FIELDS[key]
    try:
        value = str(feishu_global_rule_policy().get(key) or "")
    except Exception:  # noqa: BLE001 - 文案读取永不影响主流程
        value = ""
    return value or os.getenv(env_name, default)


def processing_ack_text() -> str:
    return _rule_text("处理中文案")


def processing_remind_text() -> str:
    return _rule_text("追问文案")


def processing_empty_fallback_text() -> str:
    return _rule_text("空回复文案")


def send_processing_card(details: dict[str, Any], text: str) -> str | None:
    """Send a footer-less '正在处理' placeholder card as a reply to the user's message.
    Returns the placeholder message_id, or None if any prerequisite/step fails
    (best-effort: a failure here must never affect the real answer)."""
    if not feishu_app_credentials()[0]:
        return None
    try:
        auth_token = feishu_tenant_access_token()
    except Exception as exc:
        log_line(f"processing card: token error: {exc}")
        return None
    if not auth_token:
        return None
    try:
        card = build_feishu_card([("text", text)], footer="")
        return feishu_send_interactive_message(details, feishu_message_id(details), card, auth_token) or None
    except Exception as exc:
        log_line(f"processing card send failed: {exc}")
        return None


class _PlaceholderRotation:
    """占位卡轮播状态：lock 串行化轮播 patch 与终局 patch，cancelled 置位后轮播立即停。"""

    def __init__(self) -> None:
        self.lock = Lock()
        self.cancelled = False


_placeholder_rotations: dict[str, _PlaceholderRotation] = {}
_placeholder_rotations_lock = Lock()

PLACEHOLDER_ROTATE_MAX_SECONDS = 1800.0  # 安全上限：终局 patch 丢失时轮播也不会永转


def placeholder_rotation_enabled() -> bool:
    return bool(feishu_global_rule_policy().get("占位轮播"))


def placeholder_rotate_seconds() -> float:
    try:
        return max(3.0, float(os.getenv("OPENCLAW_BRIDGE_PLACEHOLDER_ROTATE_SECONDS", "8")))
    except ValueError:
        return 8.0


def placeholder_rotation_tips() -> list[str]:
    """轮播提示列表：规则表「轮播提示文案」一行一条（>env>默认），空行忽略。"""
    return [line.strip() for line in _rule_text("轮播提示文案").splitlines() if line.strip()]


def stop_placeholder_rotation(message_id: str) -> None:
    with _placeholder_rotations_lock:
        entry = _placeholder_rotations.pop(str(message_id), None)
    if entry:
        # 拿到 lock 才返回：此刻在途的轮播 patch 已落地，之后的终局 patch 必然后到、必然赢。
        with entry.lock:
            entry.cancelled = True


def start_placeholder_rotation(message_id: str, base_text: str) -> None:
    """占位卡轮播：每隔 N 秒就地 patch 占位卡，轮换 基础文案+提示文案 并附等待秒数。
    best-effort：任何失败只影响轮播本身，终局答案由 feishu_patch_card 的掐停逻辑兜底。"""
    tips = placeholder_rotation_tips()
    if not tips or not placeholder_rotation_enabled():
        return
    entry = _PlaceholderRotation()
    with _placeholder_rotations_lock:
        _placeholder_rotations[str(message_id)] = entry
    Thread(
        target=_placeholder_rotation_loop,
        args=(str(message_id), base_text, entry),
        daemon=True,
        name=f"placeholder-rotation-{str(message_id)[-8:]}",
    ).start()


def _placeholder_rotation_loop(message_id: str, base_text: str, entry: _PlaceholderRotation) -> None:
    texts = [base_text] + placeholder_rotation_tips()
    interval = placeholder_rotate_seconds()
    started = time.monotonic()
    index = 0
    failures = 0
    try:
        while time.monotonic() - started < PLACEHOLDER_ROTATE_MAX_SECONDS:
            time.sleep(interval)
            index += 1
            with entry.lock:
                if entry.cancelled:
                    return
                elapsed = int(time.monotonic() - started)
                text = f"{texts[index % len(texts)]}\n\n⏳ 已等待 {elapsed}s"
                try:
                    auth_token = feishu_tenant_access_token()
                    card = build_feishu_card([("text", text)], footer="")
                    feishu_patch_card(message_id, card, auth_token, _from_rotation=True)
                    failures = 0
                except Exception as exc:  # noqa: BLE001 - 轮播失败绝不影响主链路
                    failures += 1
                    log_line(f"placeholder rotation patch failed: {exc}")
                    if failures >= 3:
                        return
    finally:
        with _placeholder_rotations_lock:
            _placeholder_rotations.pop(message_id, None)


def lane_batch_key(metadata: dict[str, Any]) -> str:
    peer_id = metadata.get("peer_id")
    if not peer_id:
        return ""
    if metadata.get("channel") == "feishu":
        return "feishu:" + str(peer_id)
    parts = [
        str(metadata.get("wechat_account") or "default"),
        str(metadata.get("chat_type") or "private"),
        str(peer_id),
    ]
    if metadata.get("channel") == "wecom":
        parts.insert(0, "wecom")
    return "|".join(parts)


def done_marker_text() -> str:
    return _rule_text("完成标记")


def build_feishu_trailer(details: dict[str, Any]) -> str:
    """Content for OpenClaw's mandatory final-reply bubble (posted after the bridge's
    card). Debug-trailer ON -> a one-line link diagnostic; OFF -> a calm done marker.
    Never raises: any failure degrades to the done marker."""
    if not debug_trailer_enabled():
        return done_marker_text()
    try:
        metadata = details.get("metadata") or {}
        lane = lane_batch_key(metadata)
        busy = _inflight_counts.get(lane, 0)
        conv = str(metadata.get("chatgpt_conversation_url") or "")
        conv_tail = conv.rsplit("/", 1)[-1] if conv else "-"
        req = str(details.get("request_id") or "-")
        tag = os.getenv("OPENCLAW_BRIDGE_TAG") or "unknown"
        patched = "yes" if details.get("feishu_placeholder_msg_id") else "no"
        model = os.getenv("WEB_DOCK_MODEL", "browser-chatgpt")
        return (
            f"🔧 bridge={tag} req={req} conv={conv_tail} | "
            f"busy={busy} lane={lane or '-'} | "
            f"model={model} timeout={webdock_timeout()}s patched={patched}"
        )
    except Exception as exc:
        log_line(f"feishu trailer build failed: {exc}")
        return done_marker_text()


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
    lowered = {str(key).lower(): value for key, value in metadata.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def wecom_business_configured() -> bool:
    return bool(os.getenv("OPENCLAW_INTERNAL_TOKEN", "").strip())


def _wecom_business_url(path: str) -> str:
    base = os.getenv("WECOM_ASSISTANT_API_BASE", "http://127.0.0.1:8000/v1/internal/wecom").rstrip("/")
    return base + "/" + path.lstrip("/")


def _wecom_business_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        _wecom_business_url(path),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": os.getenv("OPENCLAW_INTERNAL_TOKEN", ""),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result if isinstance(result, dict) else {}


def _wecom_image_urls(details: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for part in details.get("images") or []:
        image_url = part.get("image_url") if isinstance(part, dict) else None
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url, str) and url.startswith("data:image/"):
            urls.append(url)
    return urls


def wecom_business_preflight(details: dict[str, Any]) -> dict[str, Any]:
    metadata = details.get("metadata") or {}
    if metadata.get("channel") != "wecom" or not wecom_business_configured():
        return {"action": "continue", "reply": ""}
    raw = details.get("raw_metadata") or {}
    peer_id = _strip_lane_peer_prefix(metadata.get("peer_id"))
    chat_type = str(metadata.get("chat_type") or "private").lower()
    sender = _first_metadata_value(raw, "sender_id", "from_user_id", "user_id", "SenderId") or ""
    msgid = _first_metadata_value(metadata, "message_id", "messageId", "msgid") or details.get("request_id")
    payload = {
        "msgid": str(msgid),
        "account_id": str(metadata.get("wechat_account") or "company-b"),
        "chatid": str(peer_id),
        "chattype": "group" if _is_group_chat(chat_type) else "private",
        "from_userid": str(sender),
        "text_content": str(details.get("user_text") or ""),
        "images": _wecom_image_urls(details),
        "raw_metadata": {
            "message_id": str(msgid),
            "sender_id": str(sender),
            "chat_type": chat_type,
        },
    }
    try:
        return _wecom_business_post("inbound", payload)
    except Exception as exc:
        log_line(f"wecom business preflight unavailable: {type(exc).__name__}")
        if re.search(r"#(?:绑定|节点|AI节点|确认节点|取消节点)", payload["text_content"], re.IGNORECASE):
            return {"action": "reply", "reply": "企微业务处理暂不可用，请稍后重试该命令。"}
        return {"action": "continue", "reply": ""}


def wecom_business_store_ai_result(draft_msgid: str, result_text: str) -> str:
    try:
        result = _wecom_business_post(
            "ai-result",
            {"draft_msgid": draft_msgid, "result_text": result_text},
        )
        return str(result.get("reply") or "").strip()
    except Exception as exc:
        log_line(f"wecom AI draft result store failed: {type(exc).__name__}")
        return ""


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


def extract_webdock_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        return {}
    result: dict[str, Any] = {}
    conversation_url = metadata.get("chatgpt_conversation_url")
    if conversation_url:
        result["chatgpt_conversation_url"] = str(conversation_url)
    project_url = metadata.get("chatgpt_project_url")
    if project_url:
        result["chatgpt_project_url"] = str(project_url)
    return result


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
    # Leave FILE: markers intact here; build_reply delivers them as native Feishu
    # files (deliver_feishu_files) and only falls back to the MEDIA rewrite when
    # direct delivery is unavailable.
    return text.strip()


def split_file_markers(text: str) -> tuple[str, list[dict[str, str]]]:
    files: list[dict[str, str]] = []

    def _remove(match: re.Match[str]) -> str:
        files.append({
            "url": match.group("url"),
            "name": urllib.parse.unquote(match.group("name")),
            "mime": urllib.parse.unquote(match.group("mime")),
        })
        return ""

    body = FILE_MARKER_RE.sub(_remove, text or "")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, files


def rewrite_file_markers_as_media(text: str) -> str:
    body, files = split_file_markers(text)
    if not files:
        return text.strip()
    parts = [body] if body else []
    parts.extend(f"MEDIA: {item['url']}" for item in files)
    return "\n".join(parts).strip()


def visible_file_fallback(body: str, files: list[dict[str, str]]) -> str:
    """Native Feishu delivery failed. The marker URL is WebDock's internal address
    (reachable from this host only, see fetch_outbound_file_bytes), so handing it
    to the user would be a dead link and an internal-address leak — name the file
    and say it failed instead."""
    parts = [body] if body else []
    parts.extend(f"📎 {item.get('name') or 'file'}（发送失败，请重试）" for item in files)
    return "\n".join(parts).strip()


def split_media_markers(text: str) -> tuple[str, list[str]]:
    urls: list[str] = []

    def _remove(match: re.Match[str]) -> str:
        urls.append(match.group("url"))
        return ""

    body = MEDIA_MARKER_RE.sub(_remove, text or "")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, urls


def split_ordered_segments(text: str) -> list[tuple[str, str]]:
    """Split a reply into ordered ('text', md) / ('image', url) segments by the
    position of each MEDIA: marker, preserving document order (unlike
    split_media_markers, which collects every url separately at the end)."""
    segments: list[tuple[str, str]] = []
    idx = 0
    for match in MEDIA_MARKER_RE.finditer(text or ""):
        pre = (text[idx:match.start()] or "").strip()
        if pre:
            segments.append(("text", pre))
        segments.append(("image", match.group("url")))
        idx = match.end()
    tail = (text[idx:] or "").strip()
    if tail:
        segments.append(("text", tail))
    return segments


def build_feishu_card(segments: list[tuple[str, str]], footer: str = "") -> dict[str, Any]:
    """Build a Feishu interactive card whose elements interleave markdown text and
    images in document order — a single coherent message instead of separate image
    bubbles. Image segments must already be resolved to image_keys; empty text is
    dropped. A non-empty ``footer`` renders as a gray note element at the bottom."""
    elements: list[dict[str, Any]] = []
    for kind, value in segments:
        if kind == "image":
            elements.append(
                {
                    "tag": "img",
                    "img_key": value,
                    "alt": {"tag": "plain_text", "content": "图片"},
                    "mode": "fit_horizontal",
                    "preview": True,
                }
            )
        elif value.strip():
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _lark_md(value)}})
    if footer.strip():
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": footer.strip()}]})
    return {"config": {"wide_screen_mode": True, "update_multi": True}, "elements": elements}


PROJECT_SLUG_RE = re.compile(r"/g/g-p-[0-9a-f]+-([^/?#]+)")


def format_card_footer(details: dict[str, Any]) -> str:
    """Developer footer for the Feishu card, in the OpenClaw gray-tail style:
    ``设备: webdock1(主) | 项目: lark-hao | 模式: 极速 | 耗时: 129s``. Parts whose
    info is missing are dropped; returns "" when nothing is known."""
    info = details.get("webdock_footer") or {}
    parts: list[str] = []
    device = str(info.get("device") or "").strip()
    if device:
        route = str(info.get("route") or "").strip()
        route_label = {"primary": "(主)", "standby": "(备)"}.get(route, "")
        parts.append(f"设备: {device}{route_label}")
    metadata = details.get("metadata") or {}
    url = str(metadata.get("chatgpt_project_url") or metadata.get("chatgpt_conversation_url") or "")
    slug = PROJECT_SLUG_RE.search(url)
    if slug:
        parts.append(f"项目: {slug.group(1)}")
    mode_value = str(metadata.get("chatgpt_mode") or "").strip()
    mode_name = CHATGPT_MODE_NAMES.get(mode_value, "")
    if mode_name:
        parts.append(f"模式: {mode_name}")
    elapsed = info.get("elapsed_seconds")
    if isinstance(elapsed, (int, float)) and elapsed > 0:
        parts.append(f"耗时: {int(elapsed)}s")
    return " | ".join(parts)


def _lark_md(text: str) -> str:
    """Adapt markdown to Feishu lark_md: ATX headings (## X) render as literal text,
    so turn them into bold, which lark_md does render."""
    return re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$", r"**\1**", text)


def visible_media_fallback(body: str, urls: list[str]) -> str:
    """Same rationale as visible_file_fallback — the MEDIA URL is internal-only,
    so report the failure rather than leaking a dead address."""
    parts = [body] if body else []
    parts.extend("🖼️ 图片发送失败，请重试" for _ in urls)
    return "\n".join(parts).strip()


_WECOM_MEDIA_NOISE_LINES = frozenset({"Edit", "编辑", "预览", "分享", "重试", "下载"})


def _clean_wecom_media_caption(text: str) -> str:
    lines = [
        line
        for line in (text or "").splitlines()
        if line.strip() and line.strip() not in _WECOM_MEDIA_NOISE_LINES
    ]
    return "\n".join(lines).strip()


def _wecom_card_plain_text(text: str) -> str:
    text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", text or "")
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`~]", "", text)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]+", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _wecom_response_url_text(text: str) -> str:
    """Render ChatGPT markdown as readable WeCom stream text.

    response_url streams show markdown punctuation literally. Widget/table
    visuals are delivered as inline images, so their pipe-table source is
    removed instead of being flattened into unreadable text.
    """
    lines: list[str] = []
    for raw_line in (text or "").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            continue
        if stripped.count("|") >= 2 or re.fullmatch(r"[|:\-\s]+", stripped or "x"):
            continue
        line = _wecom_card_plain_text(raw_line)
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        line = re.sub(r"^[\-•·]\s*", "• ", line)
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def deliver_wecom_response_url_reply(reply: str, details: dict[str, Any]) -> str:
    """Keep all visible text and MEDIA markers for one response_url stream."""
    metadata = details.get("metadata") or {}
    if metadata.get("channel") != "wecom":
        return reply
    body, urls = split_media_markers(reply)
    caption = _clean_wecom_media_caption(body)
    parts = [_wecom_response_url_text(caption)] if caption else []
    parts.extend(f"MEDIA: {url}" for url in urls[:4])
    return "\n".join(part for part in parts if part).strip()


def deliver_feishu_files(reply: str, details: dict[str, Any]) -> str:
    """Send any FILE: markers in a Feishu reply as native Feishu file messages
    (im/v1/files + msg_type=file) and return the remaining text.

    OpenClaw's legacy ``MEDIA:`` directive does not deliver files/images on Feishu
    (upstream issue #48891, closed as not planned), so we push the file straight to
    the user via the Feishu API. Falls back to the MEDIA rewrite when Feishu
    credentials are missing or every upload fails, so the file is never lost."""
    body, files = split_file_markers(reply)
    if not files:
        return reply
    metadata = details.get("metadata") or {}
    if metadata.get("channel") != "feishu":
        return rewrite_file_markers_as_media(reply)
    if not feishu_app_credentials()[0]:
        return visible_file_fallback(body, files)
    try:
        auth_token = feishu_tenant_access_token()
    except Exception as exc:  # token fetch failed; keep the legacy fallback
        log_line(f"feishu file delivery: token error: {exc}")
        auth_token = ""
    if not auth_token:
        return visible_file_fallback(body, files)
    message_id = feishu_message_id(details)
    delivered: list[str] = []
    for item in files:
        name = item.get("name") or "file"
        try:
            data = fetch_outbound_file_bytes(item["url"])
            if not data or len(data) > FEISHU_MAX_FILE_BYTES:
                log_line(f"feishu file delivery: skip {name} (size {len(data) if data else 0})")
                continue
            file_key = feishu_upload_file(data, name, feishu_file_type_for(name), auth_token)
            feishu_send_file_message(details, message_id, file_key, auth_token)
            delivered.append(name)
        except Exception as exc:
            log_line(f"feishu file delivery failed for {name}: {exc}")
    if not delivered:
        return visible_file_fallback(body, files)
    body = body.strip()
    if body:
        return body
    # File(s) sent but no accompanying text — give OpenClaw a visible caption so it
    # does not emit the "no-visible-reply" fallback.
    return "📎 " + "、".join(delivered)


def deliver_feishu_media(reply: str, details: dict[str, Any]) -> str:
    """Deliver ``MEDIA:`` image markers as ONE Feishu interactive card whose text and
    images interleave in document order (a single coherent message, matching the web
    layout) instead of separate image bubbles sent before the text.

    Returns NO_REPLY once the card is sent so OpenClaw does not also emit the text.
    Falls back to visible link text (via OpenClaw) when credentials/token are missing
    or every image upload fails, so an image is never silently lost."""
    segments = split_ordered_segments(reply)
    if not any(kind == "image" for kind, _ in segments):
        return reply
    metadata = details.get("metadata") or {}
    if metadata.get("channel") != "feishu":
        return reply
    body, urls = split_media_markers(reply)
    if not feishu_app_credentials()[0]:
        return visible_media_fallback(body, urls)
    try:
        auth_token = feishu_tenant_access_token()
    except Exception as exc:
        log_line(f"feishu card delivery: token error: {exc}")
        auth_token = ""
    if not auth_token:
        return visible_media_fallback(body, urls)
    resolved: list[tuple[str, str]] = []
    delivered = 0
    for kind, value in segments:
        if kind != "image":
            resolved.append((kind, value))
            continue
        try:
            data = fetch_outbound_file_bytes(value)
            if not data or len(data) > FEISHU_MAX_FILE_BYTES:
                raise RuntimeError(f"invalid image size {len(data) if data else 0}")
            resolved.append(("image", feishu_upload_image(data, auth_token)))
            delivered += 1
        except Exception as exc:
            log_line(f"feishu image upload failed for {value}: {exc}")
            resolved.append(("text", "🖼️ 图片发送失败，请重试"))
    if not delivered:
        return visible_media_fallback(body, urls)
    try:
        card = build_feishu_card(resolved, footer=format_card_footer(details))
        feishu_put_card(details, card, auth_token)
        return NO_REPLY
    except Exception as exc:
        log_line(f"feishu card delivery failed: {exc}")
        return visible_media_fallback(body, urls)


def deliver_feishu_text_card(reply: str, details: dict[str, Any]) -> str:
    """Send text-only Feishu replies as an interactive card too, so they carry the
    same gray developer footer as media cards. Any missing prerequisite (channel,
    footer info, credentials) or send failure falls back to the plain OpenClaw
    path, so a reply is never lost."""
    if reply == NO_REPLY or not (reply or "").strip() or reply == FALLBACK_MESSAGE:
        return reply
    metadata = details.get("metadata") or {}
    if metadata.get("channel") != "feishu":
        return reply
    footer = format_card_footer(details)
    if not footer:
        return reply
    if not feishu_app_credentials()[0]:
        return reply
    try:
        auth_token = feishu_tenant_access_token()
    except Exception as exc:
        log_line(f"feishu text card delivery: token error: {exc}")
        return reply
    if not auth_token:
        return reply
    try:
        card = build_feishu_card([("text", reply)], footer=footer)
        feishu_put_card(details, card, auth_token)
        return NO_REPLY
    except Exception as exc:
        log_line(f"feishu text card delivery failed: {exc}")
        return reply


def finalize_placeholder(reply: str, details: dict[str, Any]) -> str:
    """Guarantee a sent processing-card placeholder is always resolved. If the
    delivery chain already patched it, ``reply`` is NO_REPLY and this is a no-op.
    Otherwise patch the placeholder with the final text (or an empty-reply fallback)
    so it never stays stuck on '正在处理'. Best-effort: patch failure leaves ``reply``
    to be emitted the normal way."""
    placeholder_id = details.get("feishu_placeholder_msg_id")
    if not placeholder_id or reply == NO_REPLY:
        return reply
    text = (reply or "").strip() or processing_empty_fallback_text()
    try:
        auth_token = feishu_tenant_access_token()
        card = build_feishu_card([("text", text)], footer=format_card_footer(details))
        feishu_patch_card(placeholder_id, card, auth_token)
        return NO_REPLY
    except Exception as exc:
        log_line(f"feishu placeholder finalize failed: {exc}")
        return reply


def media_proxy_headers(headers: Any) -> dict[str, str]:
    out = {"Content-Type": headers.get("Content-Type", "application/octet-stream")}
    content_disposition = headers.get("Content-Disposition")
    if content_disposition:
        out["Content-Disposition"] = content_disposition
    return out


def diagnostic_message(
    reason: str,
    stop_at: str,
    *,
    error_code: str | None = None,
    debug_dir: str | None = None,
    elapsed_seconds: float | None = None,
    details: dict[str, Any] | None = None,
) -> str:
    """The card shown when a turn fails.

    Lines 2-3 route the problem to a component. The forensics line exists so a
    screenshot of the card is enough to find the failing turn — error code names
    the failure mode, elapsed says whether it hit a cap, the snapshot path points
    straight at the page dump, and device/time narrow the log search. Without it
    every report starts with a round of "which message, when, on which box".
    """
    lines = [
        FALLBACK_MESSAGE,
        f"诊断：OpenClaw -> openclaw-bridge 已联通；{reason}",
        f"停止点：{stop_at}。",
    ]
    forensics: list[str] = []
    if error_code:
        forensics.append(f"错误码 {error_code}")
    if elapsed_seconds is not None:
        forensics.append(f"耗时 {int(elapsed_seconds)}s")
    if debug_dir:
        forensics.append(f"快照 {debug_dir}")
    info = (details or {}).get("webdock_footer") or {}
    device = str(info.get("device") or "").strip()
    if device:
        forensics.append(f"设备 {device}")
    request_id = str((details or {}).get("request_id") or "").strip()
    if request_id:
        forensics.append(f"请求 {request_id[:8]}")
    forensics.append(datetime.now(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S"))
    lines.append(" ｜ ".join(forensics))
    return "\n".join(lines)


def parse_http_error_detail(exc: urllib.error.HTTPError) -> dict[str, Any]:
    """WebDock's error body — ``{ok, error_code, message, debug_dir}``. The body can
    only be read once, so callers take the dict and derive the message from it
    rather than calling a second parser."""
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
    except Exception:
        return {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        return detail
    return {"message": str(detail)} if detail else {}


def parse_http_error_message(exc: urllib.error.HTTPError) -> str:
    detail = parse_http_error_detail(exc)
    return str(detail.get("message") or detail.get("error_code") or exc)


def call_webdock(body: dict[str, Any]) -> WebDockResult:
    outbound = build_webdock_body(body)
    request_id = str((outbound.get("metadata") or {}).get("request_id") or "").strip()
    data = json.dumps(outbound, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webdock_url(),
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.getenv("WEB_DOCK_API_TOKEN", ""),
            "X-Request-ID": request_id,
        },
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=webdock_timeout()) as response:
        payload = json.loads(response.read().decode("utf-8"))
        device = str(response.headers.get("X-Webdock-Device") or "").strip()
        route = str(response.headers.get("X-Webdock-Route") or "").strip()
    footer: dict[str, Any] = {"elapsed_seconds": round(time.monotonic() - started)}
    if device:
        footer["device"] = device
    if route:
        footer["route"] = route
    return WebDockResult(
        normalize_reply(extract_assistant_reply(payload)) or FALLBACK_MESSAGE,
        extract_webdock_metadata(payload),
        footer,
    )


def unpack_webdock_result(result: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(result, WebDockResult):
        return result.reply, dict(result.metadata)
    if isinstance(result, tuple) and result:
        reply = str(result[0] or "")
        metadata = result[1] if len(result) > 1 and isinstance(result[1], dict) else {}
        return reply, dict(metadata)
    return str(result or ""), {}


def maybe_feishu_mode_command_reply(details: dict[str, Any]) -> str | None:
    """/模式 命令的短路回复；非飞书或非命令返回 None（走正常链路）。"""
    if (details.get("metadata") or {}).get("channel") != "feishu":
        return None
    is_command, mode = parse_feishu_mode_command(str(details.get("user_text") or ""))
    if not is_command:
        return None
    if not mode:
        current = feishu_chat_mode(details)
        current_name = CHATGPT_MODE_NAMES.get(current, "默认（高级）")
        return f"当前对话模式：{current_name}\n用法：/模式 极速｜均衡｜高级"
    if not set_feishu_chat_mode(details, mode):
        return "无法识别当前会话，模式未修改。"
    # 模式只是写进配置表的设置项：页面上的真正切换发生在下一条消息发送前
    # （chatgpt_page.ensure_mode），所以切完立刻发的那条会慢，必须说清楚。
    return (
        f"已切换为{CHATGPT_MODE_NAMES[mode]}模式，本会话后续回复将使用该模式。\n"
        "⚠️ 这一步只改设置项，系统会在你下一次提问时才到 ChatGPT 页面实际切换，"
        "首次生效那条回复约需多等 20 秒，请耐心等待；之后恢复正常速度。"
    )


def build_reply(body: dict[str, Any]) -> str:
    # started must precede request_details: for Feishu that call scans bitable for
    # the session record, the peer project config and the chat mode, which was the
    # single largest segment of the 2026-07-28 latency report and sat entirely
    # outside the measured span.
    started = time.monotonic()
    user_text = get_last_user_message(body.get("messages"))
    if not webdock_configured():
        return f"已收到你的微信消息：{user_text}" if user_text else "已收到你的微信消息。"
    details = request_details(body)  # peer_id/channel for chain_result tracing
    trace_stage(details, "request_details_done", started)
    write_details = details  # defensive: except branches reference it before reassignment
    try:
        if details.get("metadata", {}).get("channel") == "feishu" and not feishu_should_send_chatgpt(details):
            trace_chain_result(details, started, reply="")
            append_feishu_session_console_records_async(details, "", "仅记录")
            return NO_REPLY
        mode_reply = maybe_feishu_mode_command_reply(details)
        if mode_reply is not None:
            trace_chain_result(details, started, reply=mode_reply)
            append_feishu_session_console_records_async(details, mode_reply, "已回复")
            return mode_reply
        batched_body = maybe_batch_request(body)
        if batched_body == NO_REPLY:
            return NO_REPLY  # merged into a batch leader; the leader emits chain_result
        write_details = request_details(batched_body)
        write_details["request_id"] = details.get("request_id")
        wecom_action = wecom_business_preflight(write_details)
        if wecom_action.get("action") == "reply":
            reply = str(wecom_action.get("reply") or "").strip() or "企微业务命令已处理。"
            trace_chain_result(details, started, reply=reply)
            return reply
        if wecom_action.get("action") == "ai_draft":
            ai_prompt = str(wecom_action.get("ai_prompt") or "").strip()
            if ai_prompt:
                batched_body["_bridge_user_text"] = ai_prompt
                write_details["user_text"] = ai_prompt
        lane_key = lane_batch_key(write_details.get("metadata") or {})
        is_overlap = _enter_inflight(lane_key)
        try:
            if write_details.get("metadata", {}).get("channel") == "feishu" and processing_card_enabled():
                # Placeholder is best-effort; a failure here must never block the answer.
                text = processing_remind_text() if is_overlap else processing_ack_text()
                placeholder_id = send_processing_card(write_details, text)
                if placeholder_id:
                    write_details["feishu_placeholder_msg_id"] = placeholder_id
                    start_placeholder_rotation(placeholder_id, text)
            trace_stage(write_details, "webdock_call_start", started)
            call_started = time.monotonic()
            result = call_webdock(batched_body)
            webdock_call_ms = int((time.monotonic() - call_started) * 1000)
            trace_stage(write_details, "webdock_call_end", started, webdock_call_ms=webdock_call_ms)
            reply, response_metadata = unpack_webdock_result(result)
            if wecom_action.get("action") == "ai_draft":
                if reply == FALLBACK_MESSAGE or reply.startswith(FALLBACK_MESSAGE + "\n"):
                    reply += "\n\nAI 节点草稿未生成，请稍后重新发送 #AI节点。"
                else:
                    saved_reply = wecom_business_store_ai_result(
                        str(wecom_action.get("draft_msgid") or ""),
                        reply,
                    )
                    reply = saved_reply or (reply + "\n\n⚠️ AI 草稿未能保存，请重新发送 #AI节点。")
            write_details["webdock_footer"] = dict(getattr(result, "footer", None) or {})
            if response_metadata:
                write_details.setdefault("metadata", {}).update(response_metadata)
            reply = deliver_feishu_files(reply, write_details)
            reply = deliver_wecom_response_url_reply(reply, write_details)
            reply = deliver_feishu_media(reply, write_details)
            reply = deliver_feishu_text_card(reply, write_details)
            reply = finalize_placeholder(reply, write_details)
            if (write_details.get("metadata") or {}).get("channel") == "feishu" and reply == NO_REPLY:
                reply = build_feishu_trailer(write_details)
            trace_chain_result(details, started, reply=reply, webdock_call_ms=webdock_call_ms)
            append_feishu_session_console_records_async(write_details, reply, "已回复")
            return reply
        finally:
            _exit_inflight(lane_key)
    except urllib.error.HTTPError as exc:
        elapsed = time.monotonic() - started
        if exc.code == 429:
            reply = diagnostic_message(
                "bridge -> WebDock 已联通；WebDock 返回 429 BUSY，浏览器正在处理另一条请求。",
                "WebDock browser lock",
                elapsed_seconds=elapsed,
                details=details,
            )
        elif exc.code in {401, 403}:
            reply = diagnostic_message(
                f"bridge -> WebDock 已联通；WebDock 拒绝鉴权（HTTP {exc.code}）。",
                "WebDock API token",
                elapsed_seconds=elapsed,
                details=details,
            )
        else:
            # One read of the body serves both the human sentence and the
            # forensics line — HTTPError bodies cannot be read twice.
            error_detail = parse_http_error_detail(exc)
            message = str(error_detail.get("message") or error_detail.get("error_code") or exc)
            reply = diagnostic_message(
                f"bridge -> WebDock 已联通；WebDock 返回 HTTP {exc.code}: {message}",
                "WebDock API",
                error_code=str(error_detail.get("error_code") or "") or None,
                debug_dir=str(error_detail.get("debug_dir") or "") or None,
                elapsed_seconds=elapsed,
                details=details,
            )
        reply = finalize_placeholder(reply, write_details)
        trace_chain_result(details, started, http_code=exc.code)
        append_feishu_session_console_records_async(details, reply, "失败")
        return reply
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log_line(f"webdock unavailable: {exc}")
        if isinstance(exc, TimeoutError):
            reply = diagnostic_message(
                "bridge -> WebDock 请求超时，ChatGPT 可能仍在生成或页面未完成响应。",
                "WebDock/ChatGPT timeout",
            )
        elif isinstance(exc, json.JSONDecodeError):
            reply = diagnostic_message(
                "bridge -> WebDock 已联通，但返回内容不是有效 JSON。",
                "WebDock API response",
            )
        else:
            reply = diagnostic_message(
                f"bridge -> WebDock 未联通或连接失败：{exc}",
                "ECS tunnel or WebDock API",
            )
        reply = finalize_placeholder(reply, write_details)
        trace_chain_result(details, started, error=exc)
        append_feishu_session_console_records_async(details, reply, "失败")
        return reply


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


# OpenClaw 2026.6.5's stall detector counts a stream chunk as progress only when
# its delta has NON-EMPTY content; an empty-string delta no longer resets the idle
# timer, so long WebDock replies (>~130s) were aborted as ``stalled_agent_run`` and
# shown to the user as the "no-visible-reply" fallback. A zero-width space is
# non-empty (survives str.strip()/JS trim) yet invisible in the rendered reply.
_KEEPALIVE_DELTA_CONTENT = "\u200b"


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
    push a keepalive chunk (zero-width-space delta, see _KEEPALIVE_DELTA_CONTENT)
    so OpenClaw's idle/stall timer keeps resetting instead of aborting the run as
    ``stalled_agent_run`` and dropping the real reply. ``write(bytes) -> bool``
    must return False once the client has disconnected, which stops the stream
    early."""
    if reply_fn is None:
        reply_fn = build_reply
    if keepalive is None:
        keepalive = keepalive_interval()

    result: dict[str, str] = {}

    def _run() -> None:
        try:
            result["reply"] = reply_fn(body)
        except Exception as exc:  # keep the stream alive even if the worker fails
            log_line(f"bridge stream worker error: {exc}")
            result["reply"] = FALLBACK_MESSAGE

    worker = Thread(target=_run, daemon=True)
    worker.start()

    while True:
        worker.join(timeout=keepalive)
        if not worker.is_alive():
            break
        if not write(_stream_chunk(model, delta={"content": _KEEPALIVE_DELTA_CONTENT}, finish_reason=None)):
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
                headers = media_proxy_headers(response.headers)
            self.send_response(200)
            for key, value in headers.items():
                self.send_header(key, value)
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

    def _handle_invalidate_feishu_group_policy(self) -> None:
        """Drop the cached bitable group policy so the NEXT group message refetches.

        Wired to a bitable automation: when an ops user edits the "回复模式" /
        "是否启用机器人" field on the 群配置 table, the automation POSTs here with
        the changed chat_id, and we evict that cache entry. Lets the TTL stay
        generous (cache-heavy = fewer bitable HTTP scans) without paying the
        "manual edits take a TTL window to apply" cost.

        Auth: shared secret in X-Admin-Secret header, compared with
        OPENCLAW_BRIDGE_ADMIN_SECRET env. No env -> endpoint disabled (403). An
        empty chat_id clears the whole cache (operator escape hatch).
        """
        expected = os.getenv("OPENCLAW_BRIDGE_ADMIN_SECRET") or ""
        if not expected:
            return self._json(403, {"error": "admin endpoint disabled"})
        provided = self.headers.get("X-Admin-Secret") or ""
        if not hmac.compare_digest(provided, expected):
            return self._json(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        chat_id_raw = body.get("chat_id") if isinstance(body, dict) else None
        chat_id = chat_id_raw.strip() if isinstance(chat_id_raw, str) else ""
        with _feishu_group_policy_cache_lock:
            if chat_id:
                _feishu_group_policy_cache.pop(chat_id, None)
                cleared: list[str] = [chat_id]
            else:
                cleared = sorted(_feishu_group_policy_cache.keys())
                _feishu_group_policy_cache.clear()
        log_line(
            "feishu_group_policy_invalidated "
            + json.dumps({"cleared": cleared}, ensure_ascii=False, sort_keys=True)
        )
        invalidate_global_rule_cache()
        # Also re-pull the table snapshot, so a push from bitable (automation HTTP
        # request, or an event-subscription relay) makes an edit apply on the very
        # next message instead of waiting out the refresh cycle. Done inline: the
        # caller is an automation, not a user waiting on a reply.
        refreshed, refresh_errors = refresh_feishu_bitable_snapshot()
        return self._json(
            200,
            {"ok": True, "cleared": cleared, "refreshed": refreshed, "errors": refresh_errors},
        )

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
        if self.path.rstrip("/") == "/admin/invalidate-feishu-group-policy":
            return self._handle_invalidate_feishu_group_policy()
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
        # UTC ISO8601-Z instead of BaseHTTPRequestHandler's local-time bracket form.
        log_line(fmt % args)


def get_bridge_hosts() -> list[str]:
    hosts = os.getenv("OPENCLAW_BRIDGE_HOSTS") or os.getenv("OPENCLAW_BRIDGE_HOST", "127.0.0.1")
    return [host.strip() for host in hosts.split(",") if host.strip()]


# Where the daily reconcile reports drift. Defaults to the ops group so the check
# is never silently unrouted; override per deployment.
FEISHU_ALERT_CHAT_ID = os.getenv("FEISHU_ALERT_CHAT_ID", "oc_84d1130542509e374f7ea20c13d11ca4")
# Local wall-clock time (container TZ) for the daily full reconcile — an idle hour,
# because it re-scans every table back to back.
FEISHU_BITABLE_RECONCILE_AT = os.getenv("FEISHU_BITABLE_RECONCILE_AT", "04:00")


def send_feishu_alert(text: str) -> None:
    """Post a plain-text alert to the ops group. Best effort: an alert that fails
    to send must never take down the thread that noticed the problem."""
    chat_id = FEISHU_ALERT_CHAT_ID
    if not chat_id:
        return
    try:
        token = feishu_tenant_access_token()
        if not token:
            log_line("feishu_alert_skipped no_tenant_token")
            return
        feishu_post_json(
            "/im/v1/messages?receive_id_type=chat_id",
            {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            auth_token=token,
        )
    except Exception as exc:
        log_line(f"feishu_alert_failed {exc}")


def _seconds_until(hhmm: str) -> float:
    """Seconds from now to the next local occurrence of HH:MM."""
    try:
        hour, _, minute = hhmm.partition(":")
        target_h, target_m = int(hour), int(minute or 0)
    except Exception:
        target_h, target_m = 4, 0
    now = datetime.now()
    target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


# --- 配置变更播报：每轮刷新后对比配置表，把人改了什么播到运维群 ----------------

# 只盯人工会改的配置表。会话表(session)/消息表每条消息都在写，纳入必然刷屏。
FEISHU_CONFIG_WATCH_KINDS: dict[str, str] = {"rule": "规则表", "group": "群表", "user": "用户表"}
# bridge 自己写回的运行字段：它们的变化来自消息流量而不是有人改配置，必须跳过，
# 否则每分钟都会播报一次"最近消息时间变了"。
FEISHU_CONFIG_RUNTIME_FIELDS = frozenset({
    "最近互动时间", "最近消息时间", "最近 @ 机器人时间", "最近活跃时间", "最近名称解析时间",
    "已用次数", "@机器人次数", "消息数量", "上下文摘要",
    "ChatGPT 对话标题", "ChatGPT 对话链接",
    "关联会话", "关联会话记录", "关联用户", "关联用户记录", "关联群", "关联群记录",
    "默认会话", "默认会话记录", "默认私聊会话", "默认私聊会话记录",
})
# 单条播报的行数上限与单个值的字符上限——批量编辑时只报摘要，不刷屏。
FEISHU_CONFIG_CHANGE_MAX_LINES = 20
FEISHU_CONFIG_VALUE_MAX_CHARS = 60
# 每条记录用哪个字段当人类可读名，按优先级取第一个非空的。
FEISHU_CONFIG_RECORD_NAME_FIELDS = (
    "规则名称", "群名称", "飞书用户名", "规则编号", "群编号", "用户编号", "chat_id", "open_id",
)


def feishu_config_watch_tables() -> dict[str, str]:
    """table_id -> 人类表名。未配置的表不出现，也就不会被 diff。"""
    tables: dict[str, str] = {}
    for kind, label in FEISHU_CONFIG_WATCH_KINDS.items():
        table_id = feishu_session_console_table_id(kind)
        if table_id:
            tables[table_id] = label
    return tables


def feishu_config_snapshot() -> dict[str, dict[str, dict[str, Any]]]:
    """当前内存快照里配置表的 {table_id: {record_id: fields}}，运行字段已剔除。"""
    watched = feishu_config_watch_tables()
    if not watched:
        return {}
    with _feishu_bitable_list_cache_lock:
        entries = [(key.partition("\t")[2], entry[1]) for key, entry in _feishu_bitable_list_cache.items()]
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for table_id, records in entries:
        if table_id not in watched:
            continue
        result[table_id] = {
            str(record.get("record_id")): {
                name: value
                for name, value in (record.get("fields") or {}).items()
                if name not in FEISHU_CONFIG_RUNTIME_FIELDS
            }
            for record in records
            if record.get("record_id")
        }
    return result


def _config_value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        text = "、".join(_config_value_text(item) for item in value)
    else:
        text = bitable_field_text(value)
    text = " ".join(text.split())
    if not text:
        return "（空）"
    if len(text) > FEISHU_CONFIG_VALUE_MAX_CHARS:
        text = text[:FEISHU_CONFIG_VALUE_MAX_CHARS] + "…"
    return text


def _config_record_label(fields: dict[str, Any]) -> str:
    for name in FEISHU_CONFIG_RECORD_NAME_FIELDS:
        if fields.get(name) not in (None, "", [], {}):
            return _config_value_text(fields[name])
    return "未命名记录"


def diff_feishu_config_snapshots(
    before: dict[str, dict[str, dict[str, Any]]],
    after: dict[str, dict[str, dict[str, Any]]],
) -> list[str]:
    """人类可读的配置改动行；空列表 = 这一轮没人改配置。"""
    watched = feishu_config_watch_tables()
    lines: list[str] = []
    for table_id in sorted(set(before) | set(after)):
        table_label = watched.get(table_id, table_id)
        old_records = before.get(table_id) or {}
        new_records = after.get(table_id) or {}
        for record_id in sorted(set(old_records) | set(new_records)):
            old_fields = old_records.get(record_id)
            new_fields = new_records.get(record_id)
            if old_fields is None:
                lines.append(f"＋ {table_label} 新增「{_config_record_label(new_fields or {})}」")
                continue
            if new_fields is None:
                lines.append(f"－ {table_label} 删除「{_config_record_label(old_fields)}」")
                continue
            label = _config_record_label(new_fields)
            for name in sorted(set(old_fields) | set(new_fields)):
                if old_fields.get(name) == new_fields.get(name):
                    continue
                lines.append(
                    f"✎ {table_label}「{label}」{name}："
                    f"{_config_value_text(old_fields.get(name))} → {_config_value_text(new_fields.get(name))}"
                )
    return lines


def announce_feishu_config_changes(lines: list[str]) -> None:
    """把这一轮的配置改动播到运维群。播报发生在刷新之后，所以"已生效"是事实
    而不是承诺：内存快照此刻已是新值，下一条消息就按新配置走。"""
    if not lines:
        return
    shown = lines[:FEISHU_CONFIG_CHANGE_MAX_LINES]
    body = [f"[bridge] 多维表格配置已更新（{utc_now_iso()}）", *shown]
    if len(lines) > len(shown):
        body.append(f"…另有 {len(lines) - len(shown)} 处改动未列出")
    body.append(f"✅ 已完成全量同步，共 {len(lines)} 处改动，后续消息按新配置执行")
    send_feishu_alert("\n".join(body))


def reconcile_feishu_bitable_snapshot() -> dict[str, Any]:
    """Full re-scan compared against what the snapshot was already serving.

    The refresher already polls everything, so this is not catch-up for missed
    increments — it is a health check on the refresh path itself. It catches the
    failures that a short poll cycle hides: a table renamed or unshared, tenant
    token permissions revoked, a wedged refresher whose entries quietly aged out.
    """
    before = feishu_bitable_snapshot_counts()
    ok, errors = refresh_feishu_bitable_snapshot()
    after = feishu_bitable_snapshot_counts()
    drifted = {
        table: [before.get(table), after.get(table)]
        for table in sorted(set(before) | set(after))
        if before.get(table) != after.get(table)
    }
    report = {"tables_ok": ok, "errors": errors, "drifted": drifted}
    log_line("bitable_reconcile " + json.dumps(report, ensure_ascii=False, sort_keys=True))
    if errors or drifted:
        lines = [f"[bridge] 多维表格每日核对发现异常（{utc_now_iso()}）"]
        for message in errors:
            lines.append(f"✗ 拉取失败 {message}")
        for table, (old, new) in drifted.items():
            lines.append(f"⚠ {table} 记录数 {old} → {new}（增量刷新可能有遗漏）")
        send_feishu_alert("\n".join(lines))
    return report


def _bitable_snapshot_worker() -> None:
    """Keep every tracked table warm so no message ever waits on a bitable scan,
    and run the daily reconcile when its local time comes round."""
    next_reconcile = time.monotonic() + _seconds_until(FEISHU_BITABLE_RECONCILE_AT)
    # 配置播报的基线。进程刚起时若快照是空的（磁盘快照过期被丢弃），第一轮会把
    # 每条记录都算成新增——所以基线为空时只建基线、不播报。
    config_baseline = feishu_config_snapshot()
    while True:
        try:
            if time.monotonic() >= next_reconcile:
                reconcile_feishu_bitable_snapshot()
                next_reconcile = time.monotonic() + _seconds_until(FEISHU_BITABLE_RECONCILE_AT)
            else:
                ok, errors = refresh_feishu_bitable_snapshot()
                if errors:
                    log_line(
                        "bitable_snapshot_refresh_partial "
                        + json.dumps({"ok": ok, "errors": errors}, ensure_ascii=False, sort_keys=True)
                    )
            config_after = feishu_config_snapshot()
            changes = diff_feishu_config_snapshots(config_baseline, config_after) if config_baseline else []
            config_baseline = config_after
            if changes:
                log_line(
                    "bitable_config_changed "
                    + json.dumps({"count": len(changes), "changes": changes}, ensure_ascii=False)
                )
                announce_feishu_config_changes(changes)
            save_feishu_bitable_snapshot()
        except Exception as exc:
            log_line(f"bitable_snapshot_worker_error {exc}")
        time.sleep(max(5.0, FEISHU_BITABLE_SNAPSHOT_REFRESH_SECONDS))


def start_bitable_snapshot_worker() -> None:
    if not feishu_session_console_app_token():
        log_line("bitable_snapshot_worker_disabled no_app_token")
        return
    loaded = load_feishu_bitable_snapshot()
    log_line(
        "bitable_snapshot_worker_start "
        + json.dumps(
            {
                "loaded_tables": loaded,
                "refresh_seconds": FEISHU_BITABLE_SNAPSHOT_REFRESH_SECONDS,
                "reconcile_at": FEISHU_BITABLE_RECONCILE_AT,
                "alert_chat_id": FEISHU_ALERT_CHAT_ID,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    Thread(target=_bitable_snapshot_worker, daemon=True, name="bitable-snapshot").start()


if __name__ == "__main__":
    bridge_hosts = get_bridge_hosts()
    bridge_port = int(os.getenv("OPENCLAW_BRIDGE_PORT", "18080"))
    servers = [ThreadingHTTPServer((host, bridge_port), Handler) for host in bridge_hosts]
    start_bitable_snapshot_worker()
    for server in servers[1:]:
        Thread(target=server.serve_forever, daemon=True).start()
    log_line("OpenClaw bridge listening on " + ", ".join(f"http://{host}:{bridge_port}/v1" for host in bridge_hosts))
    servers[0].serve_forever()
