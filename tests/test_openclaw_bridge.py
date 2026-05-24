from __future__ import annotations

import importlib.util
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
    assert outbound["messages"][1] == {"role": "user", "content": "真实微信消息"}
    assert "large OpenClaw runtime context" not in outbound["messages"][1]["content"]
