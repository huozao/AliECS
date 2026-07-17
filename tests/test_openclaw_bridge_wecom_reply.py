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


def test_wecom_media_card_moves_caption_inside_single_card():
    bridge = load_bridge()
    details = {"metadata": {"channel": "wecom"}}

    out = bridge.deliver_wecom_media_cards(
        "穿衣建议如下\nMEDIA: https://hydwang.xyz/media/image-2", details
    )

    assert out.startswith("```json\n")
    assert out.count("```json") == 1
    card = json.loads(out.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert card["main_title"]["desc"] == "穿衣建议如下"


def test_wecom_weather_widget_uses_semantic_card_heading():
    bridge = load_bridge()
    details = {
        "user_text": "帮我查一下今天英国伦敦的天气",
        "metadata": {"channel": "wecom"},
    }

    out = bridge.deliver_wecom_media_cards(
        "Currently 67° · Mostly clear\nMEDIA: https://hydwang.xyz/media/weather", details
    )

    card = json.loads(out.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert card["main_title"] == {"title": "天气详情", "desc": "Currently 67° · Mostly clear"}


def test_wecom_clothing_widget_uses_semantic_card_heading():
    bridge = load_bridge()
    details = {
        "user_text": "明天适合穿什么衣服",
        "metadata": {"channel": "wecom"},
    }

    out = bridge.deliver_wecom_media_cards(
        "穿衣建议如下\nMEDIA: https://hydwang.xyz/media/outfit", details
    )

    card = json.loads(out.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert card["main_title"] == {"title": "穿衣建议", "desc": "穿衣建议如下"}


def test_wecom_weather_reply_is_one_structured_card_without_markdown_text():
    bridge = load_bridge()
    details = {
        "user_text": "帮我查一下今天德国法兰克福的天气",
        "metadata": {"channel": "wecom"},
    }
    reply = """今天（**2026年7月17日**）德国法兰克福天气：
• **当前：**多云，约 **20°C**
• **全天：**约 **18–28°C**
• **上午：**多云间晴，逐渐升温
• **下午：**可能有雷阵雨，尤其约 **15:00–16:00**
• **晚上：**约 **20–22°C**，晚间仍可能有雷雨
外出建议携带雨伞，穿透气夏装，并留意临时雷雨。
MEDIA: https://hydwang.xyz/media/weather"""

    out = bridge.deliver_wecom_media_cards(reply, details)

    assert out.startswith("```json\n")
    assert out.count("```json") == 1
    assert "**" not in out
    assert "MEDIA:" not in out
    card = json.loads(out.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert card["main_title"] == {
        "title": "天气详情",
        "desc": "今天（2026年7月17日）德国法兰克福天气：",
    }
    assert card["horizontal_content_list"][0] == {
        "type": 0,
        "keyname": "当前",
        "value": "多云，约 20°C",
    }
    assert card["horizontal_content_list"][-1]["keyname"] == "晚上"
    assert card["vertical_content_list"] == [
        {"title": "补充说明", "desc": "外出建议携带雨伞，穿透气夏装，并留意临时雷雨。"}
    ]


def test_wecom_multiple_media_urls_stay_in_one_card():
    bridge = load_bridge()
    details = {"metadata": {"channel": "wecom"}}

    out = bridge.deliver_wecom_media_cards(
        "MEDIA: https://h/one\nMEDIA: https://h/two\nMEDIA: https://h/three", details
    )

    assert out.count("```json") == 1
    card = json.loads(out.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert card["card_image"]["url"] == "https://h/one"
    assert card["jump_list"] == [
        {"type": 1, "title": "查看图片 2", "url": "https://h/two"},
        {"type": 1, "title": "查看图片 3", "url": "https://h/three"},
    ]


def test_wecom_media_card_skips_other_channels():
    bridge = load_bridge()
    reply = "MEDIA: https://hydwang.xyz/media/image-3"

    assert bridge.deliver_wecom_media_cards(reply, {"metadata": {"channel": "wechat"}}) == reply
