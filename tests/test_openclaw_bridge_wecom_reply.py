from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "deploy" / "openclaw-bridge" / "openclaw_bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("openclaw_bridge_wecom_reply", BRIDGE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wecom_trailing_bot_mention_is_removed_from_webdock_prompt():
    bridge = load_bridge()
    body = {
        "messages": [{"role": "user", "content": "明天天气怎么样@统一 AI 助手"}],
        "metadata": {
            "channel": "wecom",
            "wechat_account": "company-b",
            "chat_type": "group",
            "peer_id": "group:wr_test",
        },
    }

    details = bridge.request_details(body)

    assert details["user_text"] == "明天天气怎么样"
    assert details["metadata"]["channel"] == "wecom"


def test_wecom_leading_bot_mention_is_removed():
    bridge = load_bridge()

    assert bridge.strip_wecom_bot_mention("@统一 AI 助手\n后天天气怎么样") == "后天天气怎么样"


def test_wecom_media_becomes_news_card_without_media_marker():
    bridge = load_bridge()
    reply = "Edit\nMEDIA: https://hydwang.xyz/media/image-1"
    details = {"metadata": {"channel": "wecom"}}

    out = bridge.deliver_wecom_media_cards(reply, details)

    assert "MEDIA:" not in out
    blocks = [part for part in out.split("```json\n") if '"card_type"' in part]
    assert len(blocks) == 1
    card = json.loads(blocks[0].split("\n```", 1)[0])
    assert card["card_type"] == "news_notice"
    assert card["main_title"]["title"] == "图片已生成"
    assert card["card_image"]["url"] == "https://hydwang.xyz/media/image-1"
    assert card["card_action"]["url"] == "https://hydwang.xyz/media/image-1"


def test_wecom_media_card_keeps_visible_caption_as_stream_text():
    bridge = load_bridge()
    details = {"metadata": {"channel": "wecom"}}

    out = bridge.deliver_wecom_media_cards(
        "穿衣建议如下\nMEDIA: https://hydwang.xyz/media/image-2", details
    )

    assert out.startswith("穿衣建议如下\n\n```json")


def test_wecom_media_card_skips_other_channels():
    bridge = load_bridge()
    reply = "MEDIA: https://hydwang.xyz/media/image-3"

    assert bridge.deliver_wecom_media_cards(reply, {"metadata": {"channel": "wechat"}}) == reply
