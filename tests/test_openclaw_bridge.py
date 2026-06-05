from __future__ import annotations

import importlib.util
import io
import json
import threading
import time
import urllib.error
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "deploy" / "openclaw-bridge" / "openclaw_bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("openclaw_bridge", BRIDGE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_removes_openclaw_metadata_from_last_user_message():
    bridge = load_bridge()

    text = bridge.get_last_user_message(
        [
            {
                "role": "user",
                "content": 'Conversation info (untrusted metadata):\n```json\n{"message_id":"abc"}\n```\n\n请只回复：ok',
            }
        ]
    )

    assert text == "请只回复：ok"


def test_bridge_sends_clean_prompt_to_webdock(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_MODEL", "browser-chatgpt")

    outbound = bridge.build_webdock_body(
        {
            "model": "echo",
            "messages": [
                {"role": "system", "content": "large OpenClaw runtime context"},
                {
                    "role": "user",
                    "content": 'Conversation info (untrusted metadata):\n```json\n{"chat_id":"abc"}\n```\n\n真实微信消息',
                },
            ],
            "stream": True,
        }
    )

    assert outbound["model"] == "browser-chatgpt"
    assert outbound["stream"] is False
    assert outbound["messages"] == [{"role": "user", "content": "真实微信消息"}]
    assert "large OpenClaw runtime context" not in outbound["messages"][0]["content"]


def test_bridge_forwards_openclaw_metadata_to_webdock_lane(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_MODEL", "browser-chatgpt")

    outbound = bridge.build_webdock_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Conversation info (untrusted metadata):\n"
                        "```json\n"
                        '{"wechat_account":"A","chat_type":"private","peer_id":"user-1","message_id":"msg-1"}\n'
                        "```\n\n"
                        "真实微信消息"
                    ),
                },
            ],
        }
    )

    assert outbound["messages"] == [{"role": "user", "content": "真实微信消息"}]
    assert outbound["metadata"] == {
        "wechat_account": "A",
        "chat_type": "private",
        "peer_id": "user-1",
        "message_id": "msg-1",
        "chatgpt_project": "WeChat-A",
    }


def test_bridge_forwards_inbound_image_as_vision_parts(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_MODEL", "browser-chatgpt")

    data_url = "data:image/png;base64,AAAA"
    outbound = bridge.build_webdock_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "把这张图改成卡通风格"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }
    )

    assert outbound["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "把这张图改成卡通风格"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]


def test_bridge_forwards_image_only_message_without_text_part():
    bridge = load_bridge()

    outbound = bridge.build_webdock_body(
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}]}
    )

    assert outbound["messages"][0]["content"] == [
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}}
    ]


def test_bridge_resolves_openclaw_media_uri_text_to_image_part(tmp_path, monkeypatch):
    bridge = load_bridge()
    media_file = tmp_path / "785de3ce-7429-422a-9526-4bfb63724b2d.jpg"
    media_file.write_bytes(b"\xff\xd8\xffsample-jpeg")
    monkeypatch.setenv("OPENCLAW_INBOUND_MEDIA_DIR", str(tmp_path))

    outbound = bridge.build_webdock_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "[media attached: media://inbound/785de3ce-7429-422a-9526-4bfb63724b2d.jpg (image/*)]\n"
                        "To send an image back, prefer the message tool (media/path/filePath).\n"
                        "If you must inline, use MEDIA:https://example.com/image.jpg.\n"
                        "Absolute and ~ paths only work when they stay inside your allowed file-read boundary.\n"
                        "[User sent media without caption]"
                    ),
                }
            ],
        }
    )

    assert outbound["messages"][0]["content"] == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,/9j/c2FtcGxlLWpwZWc="},
        }
    ]


def test_bridge_inherits_recent_lane_metadata_for_media_only_message(tmp_path, monkeypatch):
    bridge = load_bridge()
    media_file = tmp_path / "image-a.jpg"
    media_file.write_bytes(b"\xff\xd8\xffimage-a")
    monkeypatch.setenv("OPENCLAW_INBOUND_MEDIA_DIR", str(tmp_path))

    bridge.build_webdock_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Conversation info (untrusted metadata):\n"
                        "```json\n"
                        '{"wechat_account":"A","chat_type":"private","peer_id":"user-1"}\n'
                        "```\n\n"
                        "/新对话 帮我把这张图片背景改为纯色"
                    ),
                },
            ],
        }
    )
    outbound = bridge.build_webdock_body(
        {"messages": [{"role": "user", "content": "[media attached: media://inbound/image-a.jpg (image/*)]"}]}
    )

    assert outbound["metadata"]["wechat_account"] == "A"
    assert outbound["metadata"]["peer_id"] == "user-1"
    assert outbound["messages"][0]["content"][0]["type"] == "image_url"


def test_bridge_batches_text_then_followup_media_into_one_webdock_call(tmp_path, monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "0.3")
    monkeypatch.setenv("OPENCLAW_BRIDGE_MEDIA_INTENT_BATCH_SECONDS", "0.3")
    media_file = tmp_path / "image-a.jpg"
    media_file.write_bytes(b"\xff\xd8\xffimage-a")
    monkeypatch.setenv("OPENCLAW_INBOUND_MEDIA_DIR", str(tmp_path))
    calls: list[dict] = []

    def fake_call_webdock(body):
        calls.append(bridge.build_webdock_body(body))
        return "已按图片完成修改"

    monkeypatch.setattr(bridge, "call_webdock", fake_call_webdock)
    text_body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Conversation info (untrusted metadata):\n"
                    "```json\n"
                    '{"wechat_account":"A","chat_type":"private","peer_id":"user-1"}\n'
                    "```\n\n"
                    "/新对话 帮我把这张图片背景改为纯色，让主体更清晰"
                ),
            }
        ]
    }
    media_body = {
        "messages": [{"role": "user", "content": "[media attached: media://inbound/image-a.jpg (image/*)]"}]
    }
    text_reply: dict[str, str] = {}
    worker = threading.Thread(target=lambda: text_reply.setdefault("value", bridge.build_reply(text_body)))
    worker.start()
    time.sleep(0.05)

    media_reply = bridge.build_reply(media_body)
    worker.join(timeout=2)

    assert media_reply == bridge.NO_REPLY
    assert text_reply["value"] == "已按图片完成修改"
    assert len(calls) == 1
    content = calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "/新对话 帮我把这张图片背景改为纯色，让主体更清晰"}
    assert content[1]["type"] == "image_url"
    assert calls[0]["metadata"]["peer_id"] == "user-1"


def test_bridge_uses_longer_default_batch_window_for_image_intent(monkeypatch):
    bridge = load_bridge()
    monkeypatch.delenv("OPENCLAW_BRIDGE_BATCH_SECONDS", raising=False)
    monkeypatch.setenv("OPENCLAW_BRIDGE_MEDIA_INTENT_BATCH_SECONDS", "6.5")

    media_intent = {
        "user_text": "帮我把这张图片背景改为纯色，让主体更清晰",
        "images": [],
        "metadata": {"peer_id": "user-1"},
    }
    plain_text = {
        "user_text": "今天晚饭吃什么",
        "images": [],
        "metadata": {"peer_id": "user-1"},
    }

    assert bridge.bridge_batch_seconds(media_intent) == 6.5
    assert bridge.bridge_batch_seconds(plain_text) == 2.0


def test_bridge_flushes_shortly_after_followup_media_joins_batch(tmp_path, monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "5")
    monkeypatch.setenv("OPENCLAW_BRIDGE_MEDIA_INTENT_BATCH_SECONDS", "5")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SETTLE_SECONDS", "0.05")
    media_file = tmp_path / "image-a.jpg"
    media_file.write_bytes(b"\xff\xd8\xffimage-a")
    monkeypatch.setenv("OPENCLAW_INBOUND_MEDIA_DIR", str(tmp_path))

    def fake_call_webdock(body):
        bridge.build_webdock_body(body)
        return "done"

    monkeypatch.setattr(bridge, "call_webdock", fake_call_webdock)
    text_body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Conversation info (untrusted metadata):\n"
                    "```json\n"
                    '{"wechat_account":"A","chat_type":"private","peer_id":"user-1"}\n'
                    "```\n\n"
                    "帮我把这张图片背景改为纯色"
                ),
            }
        ]
    }
    media_body = {
        "messages": [{"role": "user", "content": "[media attached: media://inbound/image-a.jpg (image/*)]"}]
    }
    text_reply: dict[str, str] = {}
    start = time.monotonic()
    worker = threading.Thread(target=lambda: text_reply.setdefault("value", bridge.build_reply(text_body)))
    worker.start()
    time.sleep(0.05)

    assert bridge.build_reply(media_body) == bridge.NO_REPLY
    worker.join(timeout=1)

    assert text_reply["value"] == "done"
    assert time.monotonic() - start < 1


def test_bridge_batches_delayed_media_after_normal_window_for_image_intent(tmp_path, monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "0.3")
    monkeypatch.setenv("OPENCLAW_BRIDGE_MEDIA_INTENT_BATCH_SECONDS", "1.2")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SETTLE_SECONDS", "0.05")
    media_file = tmp_path / "image-a.jpg"
    media_file.write_bytes(b"\xff\xd8\xffimage-a")
    monkeypatch.setenv("OPENCLAW_INBOUND_MEDIA_DIR", str(tmp_path))
    calls: list[dict] = []

    def fake_call_webdock(body):
        calls.append(bridge.build_webdock_body(body))
        return "done"

    monkeypatch.setattr(bridge, "call_webdock", fake_call_webdock)
    text_body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Conversation info (untrusted metadata):\n"
                    "```json\n"
                    '{"wechat_account":"A","chat_type":"private","peer_id":"user-1"}\n'
                    "```\n\n"
                    "帮我把这张图片背景改为纯色，让主体更清晰"
                ),
            }
        ]
    }
    media_body = {
        "messages": [{"role": "user", "content": "[media attached: media://inbound/image-a.jpg (image/*)]"}]
    }
    text_reply: dict[str, str] = {}
    worker = threading.Thread(target=lambda: text_reply.setdefault("value", bridge.build_reply(text_body)))
    worker.start()
    time.sleep(0.7)

    media_reply = bridge.build_reply(media_body)
    worker.join(timeout=2)

    assert media_reply == bridge.NO_REPLY
    assert text_reply["value"] == "done"
    assert len(calls) == 1
    content = calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"


def test_bridge_normalizes_english_timeout_errors_to_fallback():
    bridge = load_bridge()

    payload = {
        "choices": [
            {
                "message": {
                    "content": "LLM request timed out. The model did not produce a response before the model idle timeout."
                }
            }
        ]
    }

    reply = bridge.normalize_reply(bridge.extract_assistant_reply(payload))

    assert bridge.FALLBACK_MESSAGE in reply
    assert "ChatGPT browser response extraction" in reply


def test_bridge_reports_webdock_busy_with_diagnostic(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")

    def raise_busy(*args, **kwargs):
        body = b'{"detail":{"error_code":"BUSY","message":"Browser is processing another request."}}'
        raise urllib.error.HTTPError(
            "http://127.0.0.1:11800/v1/chat/completions",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(bridge.urllib.request, "urlopen", raise_busy)

    reply = bridge.build_reply({"messages": [{"role": "user", "content": "hello"}]})

    assert bridge.FALLBACK_MESSAGE in reply
    assert "WebDock 返回 429 BUSY" in reply
    assert "WebDock browser lock" in reply


def test_bridge_hosts_accepts_comma_separated_list(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("OPENCLAW_BRIDGE_HOSTS", "127.0.0.1, 172.20.0.1")

    assert bridge.get_bridge_hosts() == ["127.0.0.1", "172.20.0.1"]


def _parse_sse(events: list[bytes]):
    """Decode collected SSE byte chunks into (parsed JSON payloads, saw_done)."""
    payloads = []
    saw_done = False
    for raw in events:
        for line in raw.decode("utf-8").splitlines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data.strip() == "[DONE]":
                saw_done = True
                continue
            payloads.append(json.loads(data))
    return payloads, saw_done


def test_stream_sse_emits_keepalives_then_final_reply():
    bridge = load_bridge()
    events: list[bytes] = []

    def write(data: bytes) -> bool:
        events.append(data)
        return True

    def slow_reply(body):
        time.sleep(0.25)
        return "最终回复"

    bridge.stream_sse(write, {"messages": []}, "browser-chatgpt", reply_fn=slow_reply, keepalive=0.05)

    payloads, saw_done = _parse_sse(events)
    assert saw_done
    # keepalive chunks: empty content, not finished
    heartbeats = [
        p for p in payloads
        if p["choices"][0]["delta"] == {"content": ""} and p["choices"][0]["finish_reason"] is None
    ]
    assert len(heartbeats) >= 1
    # the real reply is delivered after the wait
    contents = [p["choices"][0]["delta"].get("content") for p in payloads]
    assert "最终回复" in [c for c in contents if c]
    # stream is closed with a stop chunk
    assert any(p["choices"][0]["finish_reason"] == "stop" for p in payloads)


def test_stream_sse_stops_when_client_disconnects():
    bridge = load_bridge()
    calls = {"n": 0}

    def write(data: bytes) -> bool:
        calls["n"] += 1
        return False  # OpenClaw already gone on the first keepalive

    def slow_reply(body):
        time.sleep(0.3)
        return "should never be sent"

    bridge.stream_sse(write, {}, "m", reply_fn=slow_reply, keepalive=0.05)

    # one keepalive write returned False -> bail out, no content/done writes
    assert calls["n"] == 1


def test_stream_sse_uses_fallback_when_reply_fn_raises():
    bridge = load_bridge()
    events: list[bytes] = []

    def write(data: bytes) -> bool:
        events.append(data)
        return True

    def boom(body):
        raise RuntimeError("webdock down")

    bridge.stream_sse(write, {}, "m", reply_fn=boom, keepalive=5)

    payloads, saw_done = _parse_sse(events)
    assert saw_done
    contents = [c for c in (p["choices"][0]["delta"].get("content") for p in payloads) if c]
    assert bridge.FALLBACK_MESSAGE in contents


def test_webdock_timeout_default_covers_long_chatgpt(monkeypatch):
    bridge = load_bridge()
    monkeypatch.delenv("WEB_DOCK_TIMEOUT_SECONDS", raising=False)
    # must outlast WebDock's prod chat_timeout (~300s) so the bridge waits for
    # the real reply instead of timing out first
    assert bridge.webdock_timeout() >= 300


def test_keepalive_interval_well_under_openclaw_idle(monkeypatch):
    bridge = load_bridge()
    monkeypatch.delenv("OPENCLAW_BRIDGE_KEEPALIVE_SECONDS", raising=False)
    assert 0 < bridge.keepalive_interval() <= 30
