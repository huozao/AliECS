from __future__ import annotations

import importlib.util
import io
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
