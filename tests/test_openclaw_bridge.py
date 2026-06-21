from __future__ import annotations

import importlib.util
import io
import json
import threading
import time
import urllib.error
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "deploy" / "openclaw-bridge" / "openclaw_bridge.py"
BITABLE_MIGRATION_PATH = ROOT / "deploy" / "openclaw-bridge" / "migrate_feishu_bitable_links.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("openclaw_bridge", BRIDGE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_bitable_migration():
    spec = importlib.util.spec_from_file_location("migrate_feishu_bitable_links", BITABLE_MIGRATION_PATH)
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


def test_bridge_strips_feishu_bot_mention_helper_before_webdock():
    bridge = load_bridge()
    helper = (
        '[System: The content may include mention tags in the form '
        '<at user_id="...">name</at>. Treat these as real mentions of Feishu entities (users or bots).]\n'
        '[System: If user_id is "ou_bot", that mention refers to you.]'
    )
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"@hao的智能助手 请处理这张图片\n\n{helper}"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,UE5H"}},
                ],
            }
        ],
        "metadata": {
            "channel": "feishu",
            "chat_type": "group",
            "chat_id": "oc_group_clean",
            "was_mentioned": True,
        },
    }

    details = bridge.request_details(body)

    assert details["user_text"] == "请处理这张图片"
    assert len(details["images"]) == 1


def test_bridge_removes_unfenced_openclaw_metadata_from_last_user_message():
    bridge = load_bridge()

    text = bridge.get_last_user_message(
        [
            {
                "role": "user",
                "content": (
                    "[Fri 2026-06-12 02:55 UTC] Conversation info (untrusted metadata):\n"
                    "json\n"
                    '{\n'
                    '  "chat_id": "o9cq80whD47YZs0xR1Y9Ih8rdVnc@im.wechat",\n'
                    '  "message_id": "openclaw-weixin:1781232935667-3a8642ac",\n'
                    '  "timestamp": "Fri 2026-06-12 02:55:35 UTC"\n'
                    "}\n"
                    "/新对话 现在几点了？"
                ),
            }
        ]
    )

    assert text == "/新对话 现在几点了？"


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


def test_bridge_forwards_unfenced_openclaw_metadata_to_webdock_lane(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_MODEL", "browser-chatgpt")

    outbound = bridge.build_webdock_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "[Fri 2026-06-12 02:55 UTC] Conversation info (untrusted metadata):\n"
                        "json\n"
                        '{\n'
                        '  "chat_id": "o9cq80whD47YZs0xR1Y9Ih8rdVnc@im.wechat",\n'
                        '  "message_id": "openclaw-weixin:1781232935667-3a8642ac",\n'
                        '  "timestamp": "Fri 2026-06-12 02:55:35 UTC"\n'
                        "}\n"
                        "/新对话 现在几点了？"
                    ),
                },
            ],
        }
    )

    assert outbound["messages"] == [{"role": "user", "content": "/新对话 现在几点了？"}]
    assert outbound["metadata"] == {
        "chat_type": "private",
        "peer_id": "o9cq80whD47YZs0xR1Y9Ih8rdVnc@im.wechat",
        "message_id": "openclaw-weixin:1781232935667-3a8642ac",
    }


def test_bridge_forwards_feishu_open_id_as_isolated_lane(monkeypatch):
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
                        '{"channel":"feishu","chat_type":"private","open_id":"ou_abc","message_id":"m-feishu-1"}\n'
                        "```\n\n"
                        "飞书私聊消息"
                    ),
                },
            ],
        }
    )

    assert outbound["messages"] == [{"role": "user", "content": "飞书私聊消息"}]
    assert outbound["metadata"] == {
        "channel": "feishu",
        "chat_type": "private",
        "peer_id": "user:ou_abc",
        "message_id": "m-feishu-1",
        "chatgpt_project": "Feishu",
    }
    assert bridge.lane_batch_key(outbound["metadata"]) == "feishu:user:ou_abc"


def test_bridge_detects_feishu_from_real_openclaw_metadata():
    bridge = load_bridge()

    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Sender (untrusted metadata):\n```json\n"
                    '{"label":"hao (ou_28d4)","id":"ou_28d4","name":"hao"}\n```\n\n'
                    "[message_id: om_x100]\nhao: 状态测试：请回复 feishu-dm-ok"
                ),
            }
        ],
        "metadata": {
            "peer_id": "user:ou_28d4",
            "message_id": "om_x100",
            "chat_type": "private",
        },
    }

    outbound = bridge.build_webdock_body(body)
    md = outbound["metadata"]

    assert md["channel"] == "feishu"
    assert md["peer_id"] == "user:ou_28d4"
    assert md["chatgpt_project"] == "Feishu"
    assert bridge.lane_batch_key(md) == "feishu:user:ou_28d4"
    content = outbound["messages"][0]["content"]
    assert "untrusted metadata" not in content
    assert "message_id" not in content
    assert "状态测试：请回复 feishu-dm-ok" in content


def test_bridge_detects_feishu_group_chat_id():
    bridge = load_bridge()

    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"peer_id": "chat:oc_6510eb", "message_id": "om_y", "chat_type": "group"},
    }

    md = bridge.build_webdock_body(body)["metadata"]

    assert md["channel"] == "feishu"
    assert md["peer_id"] == "group:oc_6510eb"


def test_bridge_detects_real_feishu_group_conversation_info():
    bridge = load_bridge()

    text = (
        "Conversation info (untrusted metadata):\n"
        "```json\n"
        "{\n"
        '  "chat_id": "chat:oc_b39807445ba47156b05666ce457e78bf",\n'
        '  "message_id": "om_group_real",\n'
        '  "sender_id": "ou_28d4f058cbd2a13f3fcc6fd575023e8e",\n'
        '  "is_group_chat": true,\n'
        '  "was_mentioned": true\n'
        "}\n"
        "```\n\n"
        "Sender (untrusted metadata):\n"
        "```json\n"
        '{"label":"hao (ou_28d4)","id":"ou_28d4","name":"hao"}\n'
        "```\n\n"
        "[message_id: om_group_real]\n"
        "hao: /新对话 现在几点了"
    )

    outbound = bridge.build_webdock_body({"messages": [{"role": "user", "content": text}]})

    assert outbound["metadata"]["channel"] == "feishu"
    assert outbound["metadata"]["chat_type"] == "group"
    assert outbound["metadata"]["peer_id"] == "group:oc_b39807445ba47156b05666ce457e78bf"
    assert outbound["metadata"]["message_id"] == "om_group_real"
    assert outbound["messages"][0]["content"] == "/新对话 现在几点了"
    assert "Sender (untrusted metadata)" not in outbound["messages"][0]["content"]


def test_bridge_uses_one_feishu_group_lane_for_all_group_members():
    bridge = load_bridge()

    first = bridge.build_webdock_body(
        {
            "messages": [{"role": "user", "content": "A 在群里说话"}],
            "metadata": {
                "channel": "feishu",
                "chat_type": "group",
                "chat_id": "oc_group1",
                "open_id": "ou_a",
                "message_id": "om_a",
            },
        }
    )["metadata"]
    second = bridge.build_webdock_body(
        {
            "messages": [{"role": "user", "content": "B 在同一个群里说话"}],
            "metadata": {
                "channel": "feishu",
                "chat_type": "group",
                "chat_id": "oc_group1",
                "open_id": "ou_b",
                "message_id": "om_b",
            },
        }
    )["metadata"]
    private = bridge.build_webdock_body(
        {
            "messages": [{"role": "user", "content": "A 私聊"}],
            "metadata": {
                "channel": "feishu",
                "chat_type": "private",
                "open_id": "ou_a",
                "message_id": "om_c",
            },
        }
    )["metadata"]

    assert first["peer_id"] == "group:oc_group1"
    assert second["peer_id"] == "group:oc_group1"
    assert private["peer_id"] == "user:ou_a"
    assert bridge.lane_batch_key(first) == bridge.lane_batch_key(second)
    assert bridge.lane_batch_key(first) != bridge.lane_batch_key(private)


def test_bridge_keeps_wechat_lane_for_real_wechat_metadata():
    bridge = load_bridge()

    body = {
        "messages": [{"role": "user", "content": "能P图嘛"}],
        "metadata": {
            "wechat_account": "default",
            "peer_id": "o9cq80whD47YZs0xR1Y9Ih8rdVnc@im.wechat",
            "chat_type": "private",
            "message_id": "123",
        },
    }

    md = bridge.build_webdock_body(body)["metadata"]

    assert "channel" not in md
    assert md["wechat_account"] == "default"
    assert md["peer_id"] == "o9cq80whD47YZs0xR1Y9Ih8rdVnc@im.wechat"
    assert bridge.lane_batch_key(md).startswith("default|")


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


def test_bridge_resolves_pdf_file_block_to_attachment(tmp_path, monkeypatch):
    # OpenClaw forwards a binary document as a <file name mime> block (body is a
    # placeholder); the real bytes live in the inbound dir under that name. The
    # bridge reads them and forwards a data-URL attachment.
    bridge = load_bridge()
    pdf_name = "report---9e7f833c-d14b-4f3e-bb17-736b63fdc799.pdf"
    (tmp_path / pdf_name).write_bytes(b"%PDF-1.4 sample document bytes")
    monkeypatch.setenv("OPENCLAW_INBOUND_MEDIA_DIR", str(tmp_path))

    outbound = bridge.build_webdock_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "看看这个文件做个海报\n"
                        f'<file name="{pdf_name}" mime="application/pdf">\n'
                        "[PDF content rendered to images; images not forwarded to model]\n"
                        "</file>"
                    ),
                }
            ],
        }
    )

    content = outbound["messages"][0]["content"]
    text_part = next(p for p in content if p["type"] == "text")
    assert "海报" in text_part["text"]
    assert "PDF content rendered" not in text_part["text"]  # noisy placeholder dropped
    img_part = next(p for p in content if p["type"] == "image_url")
    assert img_part["image_url"]["url"].startswith("data:application/pdf;base64,")


def test_bridge_skips_text_file_block(tmp_path, monkeypatch):
    # text/* files are inlined by OpenClaw, so the block must NOT be uploaded; the
    # outbound content stays a plain string with the inlined text preserved.
    bridge = load_bridge()
    txt_name = "notes.txt"
    (tmp_path / txt_name).write_bytes(b"should-not-be-uploaded")
    monkeypatch.setenv("OPENCLAW_INBOUND_MEDIA_DIR", str(tmp_path))

    outbound = bridge.build_webdock_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f'<file name="{txt_name}" mime="text/plain">\n'
                        "actual inlined content here\n"
                        "</file>"
                    ),
                }
            ],
        }
    )

    content = outbound["messages"][0]["content"]
    assert isinstance(content, str)
    assert "actual inlined content here" in content


def test_bridge_ignores_file_block_inside_conversation_history(tmp_path, monkeypatch):
    # A <file> block embedded in a <conversation> context-checkpoint is history and
    # must not be re-uploaded.
    bridge = load_bridge()
    pdf_name = "old---abc.pdf"
    (tmp_path / pdf_name).write_bytes(b"%PDF-1.4 old")
    monkeypatch.setenv("OPENCLAW_INBOUND_MEDIA_DIR", str(tmp_path))

    outbound = bridge.build_webdock_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "<conversation>\n[User]: earlier\n"
                        f'<file name="{pdf_name}" mime="application/pdf">x</file>\n'
                        "</conversation>\n请总结上面的对话"
                    ),
                }
            ],
        }
    )

    # No fresh attachment -> plain string outbound, no image parts.
    assert isinstance(outbound["messages"][0]["content"], str)


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
    avatar_reference_intent = {
        "user_text": "把一张头像改的像第二张头像的风格",
        "images": [],
        "metadata": {"peer_id": "user-1"},
    }
    plain_text = {
        "user_text": "今天晚饭吃什么",
        "images": [],
        "metadata": {"peer_id": "user-1"},
    }

    assert bridge.bridge_batch_seconds(media_intent) == 6.5
    assert bridge.bridge_batch_seconds(avatar_reference_intent) == 6.5
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


def test_bridge_waits_for_second_expected_image_before_flushing(tmp_path, monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "2")
    monkeypatch.setenv("OPENCLAW_BRIDGE_MEDIA_INTENT_BATCH_SECONDS", "2")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SETTLE_SECONDS", "0.05")
    image_a = tmp_path / "image-a.jpg"
    image_b = tmp_path / "image-b.jpg"
    image_a.write_bytes(b"\xff\xd8\xffimage-a")
    image_b.write_bytes(b"\xff\xd8\xffimage-b")
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
                    "把第一张头像改成第二张头像的风格"
                ),
            }
        ]
    }
    media_a_body = {
        "messages": [{"role": "user", "content": "[media attached: media://inbound/image-a.jpg (image/*)]"}]
    }
    media_b_body = {
        "messages": [{"role": "user", "content": "[media attached: media://inbound/image-b.jpg (image/*)]"}]
    }
    text_reply: dict[str, str] = {}
    worker = threading.Thread(target=lambda: text_reply.setdefault("value", bridge.build_reply(text_body)))
    worker.start()
    time.sleep(0.05)

    assert bridge.build_reply(media_a_body) == bridge.NO_REPLY
    time.sleep(0.15)
    assert worker.is_alive()

    assert bridge.build_reply(media_b_body) == bridge.NO_REPLY
    worker.join(timeout=1)

    assert text_reply["value"] == "done"
    assert len(calls) == 1
    content = calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "把第一张头像改成第二张头像的风格"}
    assert [part["type"] for part in content[1:]] == ["image_url", "image_url"]


def test_bridge_emits_redacted_request_trace_for_batch(monkeypatch, capsys):
    bridge = load_bridge()
    monkeypatch.setenv("OPENCLAW_BRIDGE_TRACE", "1")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "0.01")
    monkeypatch.setenv("OPENCLAW_BRIDGE_MEDIA_INTENT_BATCH_SECONDS", "0.01")
    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Conversation info (untrusted metadata):\n"
                    "```json\n"
                    '{"wechat_account":"A","chat_type":"private","peer_id":"user-1","message_id":"msg-1"}\n'
                    "```\n\n"
                    "把一张头像改的像第二张头像的风格"
                ),
            }
        ]
    }

    bridge.maybe_batch_request(body)

    lines = [line for line in capsys.readouterr().out.splitlines() if "bridge_request_trace" in line]
    assert lines
    event = json.loads(lines[-1].split(" ", 1)[1])
    assert event["event"] == "batch_flush"
    assert event["peer_id"] == "user-1"
    assert event["message_id"] == "msg-1"
    assert event["text_len"] == len("把一张头像改的像第二张头像的风格")
    assert event["image_count"] == 0
    assert event["wait_seconds"] == 0.01
    assert event["expected_images"] == 2
    assert "把一张头像" not in lines[-1]


def test_bridge_emits_chain_result_for_feishu_roundtrip(monkeypatch, capsys):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_TRACE", "1")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "0")  # passthrough, no batch wait
    monkeypatch.setattr(bridge, "call_webdock", lambda body: "已完成")

    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Sender (untrusted metadata):\n```json\n"
                    '{"name":"hao","open_id":"ou_abc","peer_id":"ou_abc"}\n```\n\n好的'
                ),
            }
        ],
        "metadata": {"channel": "feishu"},
    }

    assert bridge.build_reply(body) == "已完成"

    traces = [json.loads(l.split(" ", 1)[1]) for l in capsys.readouterr().out.splitlines() if "bridge_request_trace" in l]
    chain = [e for e in traces if e.get("event") == "chain_result"]
    assert chain, "expected a chain_result trace for the round-trip"
    ev = chain[-1]
    assert ev["result"] == "ok"
    assert ev["channel"] == "feishu"
    assert ev["peer_id"] == "user:ou_abc"
    assert ev["reply_len"] == len("已完成")


def test_bridge_writes_feishu_session_console_after_reply(monkeypatch):
    bridge = load_bridge()
    writes = []
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "0")
    monkeypatch.setattr(bridge, "call_webdock", lambda body: "飞书回复")
    monkeypatch.setattr(bridge, "append_feishu_session_console_records", lambda details, reply, status: writes.append((details, reply, status)))

    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Sender (untrusted metadata):\n```json\n"
                    '{"name":"hao","open_id":"ou_abc","peer_id":"ou_abc"}\n```\n\n你好'
                ),
            }
        ],
        "metadata": {"channel": "feishu", "message_id": "om_1"},
    }

    assert bridge.build_reply(body) == "飞书回复"

    assert writes
    details, reply, status = writes[-1]
    assert details["metadata"]["channel"] == "feishu"
    assert details["metadata"]["peer_id"] == "user:ou_abc"
    assert reply == "飞书回复"
    assert status == "已回复"


def test_bridge_passes_webdock_conversation_url_to_bitable_writer(monkeypatch):
    bridge = load_bridge()
    writes = []
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "0")
    monkeypatch.setattr(
        bridge,
        "call_webdock",
        lambda body: bridge.WebDockResult(
            "飞书回复",
            {"chatgpt_conversation_url": "https://chatgpt.com/g/g-p-lark/c/conv-1"},
        ),
    )
    monkeypatch.setattr(bridge, "append_feishu_session_console_records", lambda details, reply, status: writes.append((details, reply, status)))

    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Sender (untrusted metadata):\n```json\n"
                    '{"name":"hao","open_id":"ou_abc","peer_id":"ou_abc"}\n```\n\n你好'
                ),
            }
        ],
        "metadata": {"channel": "feishu", "message_id": "om_1"},
    }

    assert bridge.build_reply(body) == "飞书回复"

    details, _reply, _status = writes[-1]
    assert details["metadata"]["chatgpt_conversation_url"] == "https://chatgpt.com/g/g-p-lark/c/conv-1"


def test_bridge_batch_preserves_feishu_raw_metadata_for_session_index(monkeypatch):
    bridge = load_bridge()
    writes = []
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "0.01")
    monkeypatch.setenv("OPENCLAW_BRIDGE_MEDIA_INTENT_BATCH_SECONDS", "0")
    monkeypatch.setattr(
        bridge,
        "call_webdock",
        lambda body: bridge.WebDockResult(
            "飞书回复",
            {"chatgpt_conversation_url": "https://chatgpt.com/g/g-p-lark/c/conv-2"},
        ),
    )
    monkeypatch.setattr(bridge, "append_feishu_session_console_records", lambda details, reply, status: writes.append((details, reply, status)))

    body = {
        "messages": [{"role": "user", "content": "你好"}],
        "metadata": {
            "channel": "feishu",
            "tenant_key": "tenant-a",
            "open_id": "ou_abc",
            "sender_id": "ou_abc",
            "message_id": "om_1",
            "chat_type": "private",
        },
    }

    assert bridge.build_reply(body) == "飞书回复"

    details, _reply, _status = writes[-1]
    assert details["raw_metadata"]["tenant_key"] == "tenant-a"
    assert details["raw_metadata"]["open_id"] == "ou_abc"
    assert details["metadata"]["peer_id"] == "user:ou_abc"
    assert details["metadata"]["chatgpt_conversation_url"] == "https://chatgpt.com/g/g-p-lark/c/conv-2"
    assert bridge.feishu_session_key(details) == "tenant-a:user:ou_abc"


def test_bridge_records_unmentioned_feishu_group_without_webdock_or_task(monkeypatch):
    bridge = load_bridge()
    created = []
    monkeypatch.setattr(bridge, "feishu_group_reply_policy", lambda details: (True, "仅@回复"))
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "0")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_APP_TOKEN", "app_token")
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_MESSAGE_TABLE_ID", "tbl_message")
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_TASK_TABLE_ID", "tbl_task")
    monkeypatch.setattr(
        bridge,
        "call_webdock",
        lambda body: (_ for _ in ()).throw(AssertionError("non-mentioned group message must not call WebDock")),
    )
    monkeypatch.setattr(
        bridge,
        "create_feishu_bitable_record",
        lambda table_id, fields: created.append((table_id, fields)) or {},
    )

    body = {
        "messages": [{"role": "user", "content": "群里普通聊天"}],
        "metadata": {
            "channel": "feishu",
            "tenant_key": "tenant-a",
            "chat_type": "group",
            "chat_id": "oc_group1",
            "sender_id": "ou_sender",
            "message_id": "om_group_plain",
        },
    }

    assert bridge.build_reply(body) == bridge.NO_REPLY

    assert len(created) == 1
    table_id, fields = created[0]
    assert table_id == "tbl_message"
    assert fields["飞书 message_id"] == "om_group_plain"
    assert fields["是否 @ 机器人"] is False
    assert fields["是否需要送 ChatGPT"] is False
    assert fields["不处理原因"] == "未@机器人"
    assert fields["处理状态"] == "仅记录"


def test_feishu_group_without_policy_defaults_to_reply_all(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "find_feishu_bitable_record", lambda *args: None)
    details = {
        "user_text": "群里普通聊天",
        "metadata": {"channel": "feishu", "chat_type": "group", "peer_id": "group:oc_default_all"},
        "raw_metadata": {"chat_id": "oc_default_all"},
    }

    assert bridge.feishu_should_send_chatgpt(details) is True


def test_feishu_group_only_mention_policy_reads_group_table(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_GROUP_TABLE_ID", "tbl_group")
    monkeypatch.setattr(
        bridge,
        "find_feishu_bitable_record",
        lambda *args: {
            "fields": {
                "chat_id": "oc_only_at",
                "是否启用机器人": True,
                "回复模式": "仅@回复",
            }
        },
    )
    details = {
        "user_text": "群里普通聊天",
        "metadata": {"channel": "feishu", "chat_type": "group", "peer_id": "group:oc_only_at"},
        "raw_metadata": {"chat_id": "oc_only_at"},
    }

    assert bridge.feishu_should_send_chatgpt(details) is False
    details["raw_metadata"]["was_mentioned"] = True
    assert bridge.feishu_should_send_chatgpt(details) is True


def test_feishu_disabled_group_never_replies(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_GROUP_TABLE_ID", "tbl_group")
    monkeypatch.setattr(
        bridge,
        "find_feishu_bitable_record",
        lambda *args: {
            "fields": {
                "chat_id": "oc_disabled",
                "是否启用机器人": False,
                "回复模式": "回复所有",
            }
        },
    )
    details = {
        "user_text": "@机器人 仍不应回复",
        "metadata": {"channel": "feishu", "chat_type": "group", "peer_id": "group:oc_disabled"},
        "raw_metadata": {"chat_id": "oc_disabled", "wasMentioned": True},
    }

    assert bridge.feishu_should_send_chatgpt(details) is False


def test_upsert_existing_group_preserves_manual_control_fields(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_GROUP_TABLE_ID", "tbl_group")
    monkeypatch.setattr(
        bridge,
        "find_feishu_bitable_record",
        lambda *args: {
            "record_id": "rec_group",
            "fields": {
                "是否启用机器人": False,
                "是否记录全量消息": False,
                "回复模式": "仅@回复",
                "风险级别": "高",
            },
        },
    )
    updated = []
    monkeypatch.setattr(
        bridge,
        "update_feishu_bitable_record",
        lambda table_id, record_id, fields: updated.append(fields) or {},
    )
    details = {
        "user_text": "群消息",
        "metadata": {"channel": "feishu", "chat_type": "group", "peer_id": "group:oc_preserve"},
        "raw_metadata": {"chat_id": "oc_preserve", "group_name": "测试群"},
    }

    assert bridge.upsert_feishu_group_record(details) == "rec_group"
    assert updated
    for field in ("是否启用机器人", "是否记录全量消息", "回复模式", "风险级别"):
        assert field not in updated[0]


def test_new_group_defaults_to_reply_all(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_GROUP_TABLE_ID", "tbl_group")
    monkeypatch.setattr(bridge, "find_feishu_bitable_record", lambda *args: None)
    created = []
    monkeypatch.setattr(
        bridge,
        "create_feishu_bitable_record",
        lambda table_id, fields: created.append(fields) or {"data": {"record": {"record_id": "rec_group"}}},
    )
    details = {
        "user_text": "群消息",
        "metadata": {"channel": "feishu", "chat_type": "group", "peer_id": "group:oc_new"},
        "raw_metadata": {"chat_id": "oc_new"},
    }

    assert bridge.upsert_feishu_group_record(details) == "rec_group"
    assert created[0]["回复模式"] == "回复所有"


def test_feishu_message_fields_mark_group_lane():
    bridge = load_bridge()
    details = {
        "user_text": "群里 @ 机器人 的问题",
        "metadata": {
            "channel": "feishu",
            "chat_type": "group",
            "peer_id": "group:oc_group1",
            "message_id": "om_group",
        },
        "raw_metadata": {"mentions": [{"id": {"open_id": "ou_bot"}, "name": "Bot"}]},
    }

    fields = bridge.build_feishu_message_log_fields(details, reply="群回复", status="已回复")

    assert fields["聊天类型"] == "群聊"
    assert fields["群 chat_id"] == "oc_group1"
    assert fields["发送人 open_id"] == ""
    assert fields["是否 @ 机器人"] is True
    assert fields["匹配会话"] == "group:oc_group1"
    assert fields["是否需要送 ChatGPT"] is True


def test_private_feishu_message_does_not_treat_p2p_chat_id_as_group():
    bridge = load_bridge()
    details = {
        "metadata": {
            "channel": "feishu",
            "chat_type": "private",
            "peer_id": "user:ou_abc",
        },
        "raw_metadata": {"chat_id": "oc_p2p_chat", "open_id": "ou_abc"},
    }

    assert bridge.feishu_chat_id(details) == ""


def test_feishu_message_fields_group_without_mentions_is_record_only(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_group_reply_policy", lambda details: (True, "仅@回复"))
    details = {
        "user_text": "群里普通聊天",
        "metadata": {
            "channel": "feishu",
            "chat_type": "group",
            "peer_id": "group:oc_group1",
            "message_id": "om_group",
        },
    }

    fields = bridge.build_feishu_message_log_fields(details, reply="", status="仅记录")

    assert fields["聊天类型"] == "群聊"
    assert fields["是否 @ 机器人"] is False
    assert fields["是否需要送 ChatGPT"] is False
    assert fields["不处理原因"] == "未@机器人"
    assert fields["处理状态"] == "仅记录"
    assert fields["是否已回复飞书"] is False


def test_feishu_message_fields_keep_group_sender_open_id():
    bridge = load_bridge()
    details = {
        "user_text": "群里 @ 机器人 的问题",
        "metadata": {
            "channel": "feishu",
            "chat_type": "group",
            "peer_id": "group:oc_group1",
            "message_id": "om_group",
        },
        "raw_metadata": {"sender_id": "ou_sender", "mentions": [{"id": {"open_id": "ou_bot"}, "name": "Bot"}]},
    }

    fields = bridge.build_feishu_message_log_fields(details, reply="群回复", status="已回复")

    assert fields["发送人 open_id"] == "ou_sender"


def test_feishu_task_fields_include_chatgpt_input_and_reply():
    bridge = load_bridge()
    details = {
        "user_text": "/新对话 继续",
        "metadata": {
            "channel": "feishu",
            "chat_type": "private",
            "peer_id": "user:ou_abc",
            "message_id": "om_private",
        },
    }

    fields = bridge.build_feishu_reply_task_fields(details, reply="已开启", status="已发送")

    assert fields["关联消息"] == "om_private"
    assert fields["关联会话"] == "user:ou_abc"
    assert fields["任务类型"] == "新建会话"
    assert fields["任务状态"] == "已发送"
    assert fields["给 ChatGPT 的输入"] == "/新对话 继续"
    assert fields["ChatGPT 回复内容"] == "已开启"


def test_bridge_adds_current_feishu_conversation_route_from_session_index(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_CHATGPT_PROJECT_URL", "https://chatgpt.com/g/g-p-lark/project")
    monkeypatch.setattr(
        bridge,
        "find_current_feishu_session_record",
        lambda session_key: {
            "record_id": "rec_current",
            "fields": {
                "session_key": session_key,
                "ChatGPT 项目首页链接": "https://chatgpt.com/g/g-p-lark/project",
                "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark/c/conv-current",
                "会话状态": "活跃",
                "是否当前会话": True,
            },
        },
    )

    outbound = bridge.build_webdock_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Sender (untrusted metadata):\n```json\n"
                        '{"open_id":"ou_abc","tenant_key":"tenant-a","message_id":"om_1"}\n'
                        "```\n\n继续"
                    ),
                }
            ],
            "metadata": {"channel": "feishu"},
        }
    )

    assert outbound["metadata"]["chatgpt_project_url"] == "https://chatgpt.com/g/g-p-lark/project"
    assert outbound["metadata"]["chatgpt_conversation_url"] == "https://chatgpt.com/g/g-p-lark/c/conv-current"


def test_bridge_new_feishu_conversation_uses_project_and_previous_url(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_CHATGPT_PROJECT_URL", "https://chatgpt.com/g/g-p-lark/project")
    monkeypatch.setattr(
        bridge,
        "find_current_feishu_session_record",
        lambda session_key: {
            "record_id": "rec_current",
            "fields": {
                "session_key": session_key,
                "ChatGPT 项目首页链接": "https://chatgpt.com/g/g-p-lark/project",
                "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark/c/conv-old",
                "会话状态": "活跃",
                "是否当前会话": True,
            },
        },
    )

    outbound = bridge.build_webdock_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Sender (untrusted metadata):\n```json\n"
                        '{"open_id":"ou_abc","tenant_key":"tenant-a","message_id":"om_2"}\n'
                        "```\n\n/新对话 重新开始"
                    ),
                }
            ],
            "metadata": {"channel": "feishu"},
        }
    )

    assert outbound["metadata"]["chatgpt_project_url"] == "https://chatgpt.com/g/g-p-lark/project"
    assert outbound["metadata"]["previous_chatgpt_conversation_url"] == "https://chatgpt.com/g/g-p-lark/c/conv-old"
    assert "chatgpt_conversation_url" not in outbound["metadata"]


def test_feishu_session_index_archives_old_current_and_creates_new_version(monkeypatch):
    bridge = load_bridge()
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_APP_TOKEN", "app_token")
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_MESSAGE_TABLE_ID", "tbl_message")
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_SESSION_TABLE_ID", "tbl_session")
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_CHATGPT_PROJECT_URL", "https://chatgpt.com/g/g-p-lark/project")
    monkeypatch.setattr(
        bridge,
        "list_feishu_bitable_records",
        lambda table_id: [
            {
                "record_id": "rec_old",
                "fields": {
                    "session_key": "tenant-a:group:oc_group1",
                    "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark/c/conv-old",
                    "会话状态": "活跃",
                    "是否当前会话": True,
                    "会话版本": 2,
                    "消息数量": 5,
                    "@机器人次数": 5,
                },
            }
        ],
    )
    monkeypatch.setattr(bridge, "update_feishu_bitable_record", lambda table_id, record_id, fields: calls.append(("update", record_id, fields)) or {})
    monkeypatch.setattr(bridge, "create_feishu_bitable_record", lambda table_id, fields: calls.append(("create", table_id, fields)) or {})

    bridge.upsert_feishu_session_index_record(
        {
            "user_text": "/新对话 重新开始",
            "metadata": {
                "channel": "feishu",
                "chat_type": "group",
                "peer_id": "group:oc_group1",
                "message_id": "om_group",
                "chatgpt_conversation_url": "https://chatgpt.com/g/g-p-lark/c/conv-new",
            },
            "raw_metadata": {
                "tenant_key": "tenant-a",
                "chat_id": "oc_group1",
                "sender_id": "ou_sender",
                "chat_name": "项目群",
                "mentions": [{"id": {"open_id": "ou_bot"}, "name": "Bot"}],
            },
        }
    )

    assert calls[0] == ("update", "rec_old", {"会话状态": "已归档", "是否当前会话": False})
    created = calls[1][2]
    assert created["session_key"] == "tenant-a:group:oc_group1"
    assert created["会话版本"] == 3
    assert created["是否当前会话"] is True
    assert created["ChatGPT 对话链接"] == {
        "text": "https://chatgpt.com/g/g-p-lark/c/conv-new",
        "link": "https://chatgpt.com/g/g-p-lark/c/conv-new",
    }
    assert created["消息数量"] == 6
    assert created["@机器人次数"] == 6


def test_session_console_supports_master_table_ids_and_record_links(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_USER_TABLE_ID", "tbl_user")
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_GROUP_TABLE_ID", "tbl_group")
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_RULE_TABLE_ID", "tbl_rule")

    assert bridge.feishu_session_console_table_id("user") == "tbl_user"
    assert bridge.feishu_session_console_table_id("group") == "tbl_group"
    assert bridge.feishu_session_console_table_id("rule") == "tbl_rule"
    assert bridge.bitable_link_value("rec_1") == ["rec_1"]


def test_bitable_migration_defines_non_destructive_relation_fields():
    migration = load_bitable_migration()
    specs = migration.relation_field_specs(
        {
            "会话索引表": "tbl_session",
            "消息日志表": "tbl_message",
            "回复任务表": "tbl_task",
            "群表": "tbl_group",
            "用户表": "tbl_user",
            "规则配置表": "tbl_rule",
        }
    )

    assert ("会话索引表", "关联用户记录", "tbl_user") in specs
    assert ("消息日志表", "匹配会话记录", "tbl_session") in specs
    assert ("回复任务表", "关联消息记录", "tbl_message") in specs
    assert ("规则配置表", "关联会话记录", "tbl_session") in specs


def test_bitable_migration_defines_control_single_select_options():
    migration = load_bitable_migration()
    specs = {
        (table_name, field_name): options
        for table_name, field_name, options in migration.control_select_field_specs()
    }

    assert specs[("群表", "回复模式")] == ("回复所有", "仅@回复")
    assert specs[("用户表", "用户状态")] == ("启用", "停用")
    assert specs[("规则配置表", "规则对象类型")] == ("全局", "用户", "群", "会话")
    assert specs[("会话索引表", "会话状态")] == ("待创建", "活跃", "已归档")
    assert specs[("消息日志表", "命令类型")] == ("无", "/新对话", "/重置", "/摘要")
    assert specs[("消息日志表", "处理状态")] == ("已回复", "仅记录", "失败")
    assert specs[("回复任务表", "审核状态")] == ("无需审核", "待审核", "已通过", "已拒绝")
    assert not any(field_name.startswith("关联") for _, field_name in specs)


def test_bitable_migration_rejects_unknown_existing_select_value():
    migration = load_bitable_migration()

    with pytest.raises(ValueError, match="群表.回复模式.*未知模式"):
        migration.validate_control_field_values(
            "群表",
            "回复模式",
            ("回复所有", "仅@回复"),
            [{"record_id": "rec_bad", "fields": {"回复模式": "未知模式"}}],
        )


def test_bitable_migration_normalizes_reply_all_alias():
    migration = load_bitable_migration()

    assert migration.canonical_control_field_value("规则配置表", "回复模式", "全部回复") == "回复所有"
    migration.validate_control_field_values(
        "规则配置表",
        "回复模式",
        ("回复所有", "仅@回复"),
        [{"record_id": "rec_alias", "fields": {"回复模式": "全部回复"}}],
    )


def test_bitable_migration_identifies_global_and_group_default_rules():
    migration = load_bitable_migration()

    assert migration.is_default_reply_rule_id("global-default") is True
    assert migration.is_default_reply_rule_id("group-default-oc_group") is True
    assert migration.is_default_reply_rule_id("user-default-ou_user") is False


def test_bitable_single_select_payload_keeps_field_name_and_exact_options():
    migration = load_bitable_migration()

    payload = migration.single_select_field_payload("回复模式", ("回复所有", "仅@回复"))

    assert payload["field_name"] == "回复模式"
    assert payload["type"] == 3
    assert [item["name"] for item in payload["property"]["options"]] == ["回复所有", "仅@回复"]


def test_session_index_fields_use_url_objects_and_master_record_links(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_CHATGPT_PROJECT_URL", "https://chatgpt.com/g/g-p-lark/project")
    details = {
        "metadata": {
            "channel": "feishu",
            "peer_id": "user:ou_abc",
            "message_id": "om_1",
            "chatgpt_conversation_url": "https://chatgpt.com/g/g-p-lark/c/conv-1",
        },
        "raw_metadata": {"name": "hao"},
    }

    fields = bridge.build_feishu_session_index_fields(
        details,
        session_key="tenant:user:ou_abc",
        version=1,
        message_count=1,
        mention_count=0,
        user_record_id="rec_user",
        group_record_id="",
    )

    assert fields["ChatGPT 项目首页链接"] == {
        "text": "https://chatgpt.com/g/g-p-lark/project",
        "link": "https://chatgpt.com/g/g-p-lark/project",
    }
    assert fields["ChatGPT 对话链接"] == {
        "text": "https://chatgpt.com/g/g-p-lark/c/conv-1",
        "link": "https://chatgpt.com/g/g-p-lark/c/conv-1",
    }
    assert fields["关联用户记录"] == ["rec_user"]
    assert "关联群记录" not in fields


def test_upsert_feishu_user_record_creates_master_record(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_USER_TABLE_ID", "tbl_user")
    monkeypatch.setattr(bridge, "find_feishu_bitable_record", lambda *args: None)
    created = []
    monkeypatch.setattr(
        bridge,
        "create_feishu_bitable_record",
        lambda table_id, fields: created.append((table_id, fields))
        or {"data": {"record": {"record_id": "rec_user"}}},
    )
    details = {
        "metadata": {"channel": "feishu", "peer_id": "user:ou_abc"},
        "raw_metadata": {"open_id": "ou_abc", "name": "hao"},
    }

    assert bridge.upsert_feishu_user_record(details) == "rec_user"
    assert created[0][0] == "tbl_user"
    assert created[0][1]["open_id"] == "ou_abc"
    assert created[0][1]["飞书用户名"] == "hao"


def test_bridge_emits_chain_result_on_http_error(monkeypatch, capsys):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "0")

    def raise_busy(*args, **kwargs):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:11800/v1/chat/completions", 429, "Too Many Requests", {}, io.BytesIO(b"{}")
        )

    monkeypatch.setattr(bridge.urllib.request, "urlopen", raise_busy)

    bridge.build_reply({"messages": [{"role": "user", "content": "hello"}]})

    traces = [json.loads(l.split(" ", 1)[1]) for l in capsys.readouterr().out.splitlines() if "bridge_request_trace" in l]
    chain = [e for e in traces if e.get("event") == "chain_result"]
    assert chain and chain[-1]["result"] == "http_429"


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


def test_parse_file_marker():
    bridge = load_bridge()

    body, files = bridge.split_file_markers(
        "见附件\nFILE: http://webdock/media/abc name=report.pdf mime=application/pdf"
    )

    assert body.strip() == "见附件"
    assert files == [{"url": "http://webdock/media/abc", "name": "report.pdf", "mime": "application/pdf"}]


def test_normalize_reply_preserves_file_marker():
    bridge = load_bridge()

    # normalize_reply must keep FILE: intact; build_reply/deliver_feishu_files turns
    # it into a native Feishu file (OpenClaw's MEDIA: directive can't deliver files,
    # upstream issue #48891).
    reply = bridge.normalize_reply(
        "见附件\nFILE: http://webdock/media/abc name=report.pdf mime=application/pdf"
    )

    assert "FILE: http://webdock/media/abc" in reply


def test_rewrite_file_markers_as_media_fallback():
    bridge = load_bridge()

    reply = bridge.rewrite_file_markers_as_media(
        "见附件\nFILE: http://webdock/media/abc name=report.pdf mime=application/pdf"
    )

    assert reply == "见附件\nMEDIA: http://webdock/media/abc"


def test_deliver_feishu_files_sends_native_file_and_strips_marker(monkeypatch):
    bridge = load_bridge()
    sent = []
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("cli_x", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    monkeypatch.setattr(bridge, "fetch_outbound_file_bytes", lambda url: b"%PDF-1.4 data")
    monkeypatch.setattr(bridge, "feishu_upload_file", lambda data, name, ftype, token: f"key::{name}::{ftype}")
    monkeypatch.setattr(bridge, "feishu_send_file_message", lambda details, mid, key, token: sent.append((mid, key)))

    details = {"metadata": {"channel": "feishu", "message_id": "om_1"}}
    out = bridge.deliver_feishu_files(
        "已生成\nFILE: http://h/media/abc name=report.pdf mime=application/pdf", details
    )

    assert out == "已生成"  # FILE marker stripped, text kept; no MEDIA: leak
    assert sent == [("om_1", "key::report.pdf::pdf")]


def test_deliver_feishu_files_caption_when_text_empty(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("cli_x", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    monkeypatch.setattr(bridge, "fetch_outbound_file_bytes", lambda url: b"data")
    monkeypatch.setattr(bridge, "feishu_upload_file", lambda *a, **k: "key")
    monkeypatch.setattr(bridge, "feishu_send_file_message", lambda *a, **k: None)

    details = {"metadata": {"channel": "feishu", "message_id": "om_1"}}
    out = bridge.deliver_feishu_files(
        "FILE: http://h/media/abc name=scan.pdf mime=application/pdf", details
    )

    # file sent but no text -> visible caption so OpenClaw doesn't emit no-visible-reply
    assert out == "📎 scan.pdf"


def test_deliver_feishu_files_falls_back_without_credentials(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("", ""))

    details = {"metadata": {"channel": "feishu", "message_id": "om_1"}}
    out = bridge.deliver_feishu_files(
        "见附件\nFILE: http://h/media/abc name=report.pdf mime=application/pdf", details
    )

    assert out == "见附件\n附件下载：http://h/media/abc"


def test_parse_media_marker():
    bridge = load_bridge()

    body, media = bridge.split_media_markers(
        "图片已生成\nMEDIA: http://webdock/media/image-1"
    )

    assert body == "图片已生成"
    assert media == ["http://webdock/media/image-1"]


def test_deliver_feishu_media_sends_native_image_and_strips_marker(monkeypatch):
    bridge = load_bridge()
    sent = []
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("cli_x", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    monkeypatch.setattr(bridge, "fetch_outbound_file_bytes", lambda url: b"PNGDATA")
    monkeypatch.setattr(bridge, "feishu_upload_image", lambda data, token: "img_key")
    monkeypatch.setattr(
        bridge,
        "feishu_send_image_message",
        lambda details, mid, key, token: sent.append((mid, key)),
    )

    details = {"metadata": {"channel": "feishu", "message_id": "om_1"}}
    out = bridge.deliver_feishu_media(
        "图片已生成\nMEDIA: http://h/media/image-1", details
    )

    assert out == "图片已生成"
    assert sent == [("om_1", "img_key")]


def test_deliver_feishu_media_caption_when_text_empty(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("cli_x", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    monkeypatch.setattr(bridge, "fetch_outbound_file_bytes", lambda url: b"PNGDATA")
    monkeypatch.setattr(bridge, "feishu_upload_image", lambda data, token: "img_key")
    monkeypatch.setattr(bridge, "feishu_send_image_message", lambda *args: None)

    details = {"metadata": {"channel": "feishu", "message_id": "om_1"}}

    assert bridge.deliver_feishu_media("MEDIA: http://h/media/image-1", details) == "🖼️ 图片已发送"


def test_deliver_feishu_media_falls_back_to_visible_link(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("", ""))

    details = {"metadata": {"channel": "feishu", "message_id": "om_1"}}
    out = bridge.deliver_feishu_media(
        "图片已生成\nMEDIA: http://h/media/image-1", details
    )

    assert out == "图片已生成\n图片链接：http://h/media/image-1"


def test_media_proxy_headers_keep_filename():
    bridge = load_bridge()

    headers = bridge.media_proxy_headers({
        "Content-Type": "application/pdf",
        "Content-Disposition": 'attachment; filename="report.pdf"',
    })

    assert headers == {
        "Content-Type": "application/pdf",
        "Content-Disposition": 'attachment; filename="report.pdf"',
    }


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
    # keepalive chunks: NON-empty content (zero-width space) so OpenClaw 2026.6.5's
    # stall detector counts them as stream progress; an empty "" delta no longer
    # resets the idle timer and would abort long replies as stalled_agent_run.
    heartbeats = [
        p for p in payloads
        if p["choices"][0]["delta"] == {"content": bridge._KEEPALIVE_DELTA_CONTENT}
        and p["choices"][0]["finish_reason"] is None
    ]
    assert len(heartbeats) >= 1
    assert bridge._KEEPALIVE_DELTA_CONTENT != ""
    # invisible yet survives trimming (regular whitespace would be stripped to "")
    assert bridge._KEEPALIVE_DELTA_CONTENT.strip() == bridge._KEEPALIVE_DELTA_CONTENT
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
    # Must outlast WebDock's 20-minute hard cap so the bridge receives the real
    # reply or WebDock's own timeout response instead of terminating first.
    assert bridge.webdock_timeout() == 1260


def test_webdock_timeout_env_cannot_undercut_hard_cap(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_TIMEOUT_SECONDS", "320")

    assert bridge.webdock_timeout() == 1260


def test_keepalive_interval_well_under_openclaw_idle(monkeypatch):
    bridge = load_bridge()
    monkeypatch.delenv("OPENCLAW_BRIDGE_KEEPALIVE_SECONDS", raising=False)
    assert 0 < bridge.keepalive_interval() <= 30


def test_bridge_strips_feishu_sender_prefix_for_new_chat_trigger():
    # OpenClaw prefixes Feishu DM text with "<sender name>: ", which broke the
    # webdock "/新对话" trigger (it only matches text starting with /新对话).
    bridge = load_bridge()
    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Sender (untrusted metadata):\n```json\n"
                    '{"label":"hao (ou_28d4)","id":"ou_28d4","name":"hao"}\n```\n\n'
                    "[message_id: om_z]\nhao: /新对话 你好"
                ),
            }
        ],
        "metadata": {"peer_id": "user:ou_28d4", "message_id": "om_z", "chat_type": "private"},
    }
    outbound = bridge.build_webdock_body(body)
    content = outbound["messages"][0]["content"]
    text = content if isinstance(content, str) else content[0]["text"]
    assert text.startswith("/新对话")
    assert "hao:" not in text


def test_bridge_keeps_wechat_text_untouched():
    bridge = load_bridge()
    body = {
        "messages": [{"role": "user", "content": "能P图嘛"}],
        "metadata": {"wechat_account": "default", "peer_id": "o9cq80@im.wechat", "chat_type": "private"},
    }
    outbound = bridge.build_webdock_body(body)
    assert outbound["messages"][0]["content"] == "能P图嘛"


def test_bridge_cleans_feishu_multi_image_prompt_noise():
    # OpenClaw multi-image format: media-attached summary + numbered lines, a
    # message_id line (not leading), the sender prefix, and one ![image] per image.
    # The prompt forwarded to ChatGPT must contain only the user's words.
    bridge = load_bridge()
    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Sender (untrusted metadata):\n```json\n"
                    '{"label":"hao (ou_28d4)","id":"ou_28d4","name":"hao"}\n```\n\n'
                    "[media attached: 2 files]\n"
                    "[media attached 1/2: media://inbound/x.png (image/png)]\n"
                    "[media attached 2/2: media://inbound/y.png (image/png)]\n"
                    "[message_id: om_q]\n"
                    "hao: /新对话 帮我把2个图片中的人放一起\n![image]\n![image]"
                ),
            }
        ],
        "metadata": {"peer_id": "user:ou_28d4", "message_id": "om_q", "chat_type": "private"},
    }
    outbound = bridge.build_webdock_body(body)
    content = outbound["messages"][0]["content"]
    text = content if isinstance(content, str) else content[0]["text"]
    assert text == "/新对话 帮我把2个图片中的人放一起"


def test_bridge_dedups_single_inbound_image_to_one_part(monkeypatch, tmp_path):
    # One inbound image annotated BOTH as a text media-ref and an image_url part
    # (Feishu) must yield exactly ONE forwarded image part, not two.
    bridge = load_bridge()
    inbound = tmp_path / "abc.png"
    inbound.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 2048)
    monkeypatch.setenv("OPENCLAW_INBOUND_MEDIA_DIR", str(tmp_path))
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "改成卡通 [media attached: media://inbound/abc.png]"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aaaa"}},
                ],
            }
        ],
        "metadata": {"peer_id": "user:ou_28d4", "message_id": "om_a", "chat_type": "private"},
    }
    images = bridge.get_last_user_images(body["messages"])
    assert len(images) == 1


# --- /admin/invalidate-feishu-group-policy + async bitable writer ---


def _make_invalidate_handler(bridge, *, headers, body=""):
    """Handler instance wired with mock headers / rfile / _json captor.

    Bypasses BaseHTTPRequestHandler.__init__ (it expects a real socket); the
    invalidate path only touches self.headers, self.rfile, and self._json.
    """
    handler = bridge.Handler.__new__(bridge.Handler)
    handler.headers = headers
    handler.rfile = io.BytesIO(body.encode("utf-8") if isinstance(body, str) else body)
    captured: dict = {}

    def fake_json(status, obj):
        captured["status"] = status
        captured["obj"] = obj

    handler._json = fake_json
    return handler, captured


def test_invalidate_endpoint_disabled_when_secret_env_unset(monkeypatch):
    bridge = load_bridge()
    monkeypatch.delenv("OPENCLAW_BRIDGE_ADMIN_SECRET", raising=False)
    handler, captured = _make_invalidate_handler(
        bridge, headers={"X-Admin-Secret": "anything", "Content-Length": "0"}
    )
    handler._handle_invalidate_feishu_group_policy()
    assert captured["status"] == 403
    assert captured["obj"]["error"] == "admin endpoint disabled"


def test_invalidate_endpoint_rejects_wrong_secret(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("OPENCLAW_BRIDGE_ADMIN_SECRET", "correct-secret")
    handler, captured = _make_invalidate_handler(
        bridge, headers={"X-Admin-Secret": "wrong", "Content-Length": "0"}
    )
    handler._handle_invalidate_feishu_group_policy()
    assert captured["status"] == 403
    assert captured["obj"]["error"] == "forbidden"


def test_invalidate_endpoint_clears_named_chat_id(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("OPENCLAW_BRIDGE_ADMIN_SECRET", "abc")
    bridge._feishu_group_policy_cache.clear()
    bridge._feishu_group_policy_cache["oc_keep"] = (1.0, True, "回复所有")
    bridge._feishu_group_policy_cache["oc_drop"] = (1.0, True, "仅@回复")
    body = json.dumps({"chat_id": "oc_drop"})
    handler, captured = _make_invalidate_handler(
        bridge,
        headers={"X-Admin-Secret": "abc", "Content-Length": str(len(body))},
        body=body,
    )

    handler._handle_invalidate_feishu_group_policy()

    assert captured["status"] == 200
    assert captured["obj"] == {"ok": True, "cleared": ["oc_drop"]}
    assert "oc_keep" in bridge._feishu_group_policy_cache
    assert "oc_drop" not in bridge._feishu_group_policy_cache


def test_invalidate_endpoint_clears_all_when_chat_id_missing(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("OPENCLAW_BRIDGE_ADMIN_SECRET", "abc")
    bridge._feishu_group_policy_cache.clear()
    bridge._feishu_group_policy_cache["oc_a"] = (1.0, True, "回复所有")
    bridge._feishu_group_policy_cache["oc_b"] = (1.0, False, "回复所有")
    handler, captured = _make_invalidate_handler(
        bridge,
        headers={"X-Admin-Secret": "abc", "Content-Length": "2"},
        body="{}",
    )

    handler._handle_invalidate_feishu_group_policy()

    assert captured["status"] == 200
    assert sorted(captured["obj"]["cleared"]) == ["oc_a", "oc_b"]
    assert bridge._feishu_group_policy_cache == {}


def test_append_feishu_session_console_records_async_fires_in_background(monkeypatch):
    bridge = load_bridge()
    called = threading.Event()
    captured: dict = {}

    def fake_sync(details, reply, status):
        captured["details"] = details
        captured["reply"] = reply
        captured["status"] = status
        called.set()

    monkeypatch.setattr(bridge, "append_feishu_session_console_records", fake_sync)

    details_input = {"metadata": {"channel": "feishu", "peer_id": "group:oc_x"}}
    bridge.append_feishu_session_console_records_async(details_input, "hello", "已回复")

    assert called.wait(timeout=2.0), "background bitable writer never fired"
    assert captured["reply"] == "hello"
    assert captured["status"] == "已回复"
    # The wrapper deep-copies details so the caller can mutate the original
    # post-fire without poisoning the in-flight writer thread.
    details_input["metadata"]["channel"] = "wechat"
    assert captured["details"]["metadata"]["channel"] == "feishu"


def test_feishu_group_policy_cache_ttl_reads_env_at_import(monkeypatch):
    # Module-import-time env read: ops can raise the TTL without code change.
    monkeypatch.delenv("OPENCLAW_BRIDGE_FEISHU_POLICY_CACHE_SECONDS", raising=False)
    bridge = load_bridge()
    assert bridge.FEISHU_GROUP_POLICY_CACHE_SECONDS == 600.0

    monkeypatch.setenv("OPENCLAW_BRIDGE_FEISHU_POLICY_CACHE_SECONDS", "1800")
    bridge2 = load_bridge()
    assert bridge2.FEISHU_GROUP_POLICY_CACHE_SECONDS == 1800.0
