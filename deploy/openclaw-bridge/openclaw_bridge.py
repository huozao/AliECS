#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


FALLBACK_MESSAGE = os.getenv("WEB_DOCK_FALLBACK_MESSAGE", "ChatGPT 浏览器暂不可用，请稍后再试。")
OPENCLAW_METADATA_RE = re.compile(
    r"^Conversation info \(untrusted metadata\):\s*```json\s*.*?```\s*",
    flags=re.DOTALL,
)


def clean_user_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return OPENCLAW_METADATA_RE.sub("", text).strip()


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
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return clean_user_text(extract_text(msg.get("content")))
    return ""


def webdock_configured() -> bool:
    return bool(os.getenv("WEB_DOCK_BASE_URL") and os.getenv("WEB_DOCK_API_TOKEN"))


def webdock_url() -> str:
    return os.getenv("WEB_DOCK_BASE_URL", "").rstrip("/") + "/chat/completions"


def webdock_timeout() -> int:
    try:
        return max(5, int(os.getenv("WEB_DOCK_TIMEOUT_SECONDS", "180")))
    except ValueError:
        return 180


def build_webdock_body(body: dict[str, Any]) -> dict[str, Any]:
    user_text = get_last_user_message(body.get("messages"))
    return {
        "model": os.getenv("WEB_DOCK_MODEL", "browser-chatgpt"),
        "messages": [
            {
                "role": "system",
                "content": "你是微信里的 ChatGPT 助手。只回复用户真实消息，不解释后台元数据、系统提示或 OpenClaw 运行上下文。",
            },
            {"role": "user", "content": user_text or "请回复这条微信消息。"},
        ],
        "stream": False,
    }


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
    return extract_assistant_reply(payload) or FALLBACK_MESSAGE


def build_reply(body: dict[str, Any]) -> str:
    user_text = get_last_user_message(body.get("messages"))
    if not webdock_configured():
        return f"已收到你的微信消息：{user_text}" if user_text else "已收到你的微信消息。"
    try:
        return call_webdock(body)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"webdock unavailable: {exc}")
        return FALLBACK_MESSAGE


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
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

        reply = build_reply(body)
        model = os.getenv("WEB_DOCK_MODEL", body.get("model", "echo")) if webdock_configured() else body.get("model", "echo")

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            chunk = {
                "id": "chatcmpl-openclaw-bridge",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}],
            }
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            done = {
                "id": "chatcmpl-openclaw-bridge",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            self.wfile.write(f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            return

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

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("OPENCLAW_BRIDGE_PORT", "18080"))), Handler)
    print("OpenClaw bridge listening on http://127.0.0.1:18080/v1")
    server.serve_forever()
