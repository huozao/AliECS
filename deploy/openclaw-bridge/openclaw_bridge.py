#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any


FALLBACK_MESSAGE = os.getenv("WEB_DOCK_FALLBACK_MESSAGE", "ChatGPT 浏览器暂不可用，请稍后再试。")
OPENCLAW_METADATA_RE = re.compile(
    r"^Conversation info \(untrusted metadata\):\s*```json\s*.*?```\s*",
    flags=re.DOTALL,
)
OPENCLAW_METADATA_CAPTURE_RE = re.compile(
    r"^Conversation info \(untrusted metadata\):\s*```json\s*(.*?)```\s*",
    flags=re.DOTALL,
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


def clean_user_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return OPENCLAW_METADATA_RE.sub("", text).strip()


def extract_openclaw_metadata(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        return {}
    match = OPENCLAW_METADATA_CAPTURE_RE.match(text)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


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


def build_webdock_body(body: dict[str, Any]) -> dict[str, Any]:
    user_text = get_last_user_message(body.get("messages"))
    outbound = {
        "model": os.getenv("WEB_DOCK_MODEL", "browser-chatgpt"),
        "messages": [{"role": "user", "content": user_text or "请回复这条微信消息。"}],
        "stream": False,
    }
    metadata = build_webdock_metadata(body)
    if metadata:
        outbound["metadata"] = metadata
    return outbound


def build_webdock_metadata(body: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if isinstance(body.get("metadata"), dict):
        metadata.update(body["metadata"])
    metadata.update(get_last_user_metadata(body.get("messages")))

    normalized: dict[str, Any] = {}
    wechat_account = _first_metadata_value(metadata, "wechat_account", "account", "channel_id", "channel_name")
    chat_type = _first_metadata_value(metadata, "chat_type", "conversation_type", "room_type") or "private"
    peer_id = _first_metadata_value(
        metadata,
        "peer_id",
        "chat_id",
        "conversation_id",
        "from_user_id",
        "user_id",
        "sender_id",
    )

    if wechat_account:
        normalized["wechat_account"] = str(wechat_account)
        normalized["chatgpt_project"] = str(
            _first_metadata_value(metadata, "chatgpt_project", "project") or f"WeChat-{wechat_account}"
        )
    if chat_type:
        normalized["chat_type"] = str(chat_type)
    if peer_id:
        normalized["peer_id"] = str(peer_id)
    if metadata.get("message_id"):
        normalized["message_id"] = str(metadata["message_id"])
    return normalized


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
        return call_webdock(body)
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

    reply = result.get("reply") or FALLBACK_MESSAGE
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
        return self._json(
            200,
            {
                "id": "chatcmpl-openclaw-bridge",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
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
