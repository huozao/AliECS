from __future__ import annotations

import importlib.util
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


def test_wecom_media_stays_in_response_url_stream():
    bridge = load_bridge()
    reply = "Edit\nMEDIA: https://hydwang.xyz/media/image-1"
    details = {"metadata": {"channel": "wecom"}}

    out = bridge.deliver_wecom_response_url_reply(reply, details)

    assert out == "MEDIA: https://hydwang.xyz/media/image-1"


def test_wecom_response_url_reply_keeps_caption_and_media():
    bridge = load_bridge()
    details = {"metadata": {"channel": "wecom"}}

    out = bridge.deliver_wecom_response_url_reply(
        "穿衣建议如下\nMEDIA: https://hydwang.xyz/media/image-2", details
    )

    assert out == "穿衣建议如下\nMEDIA: https://hydwang.xyz/media/image-2"


def test_wecom_response_url_reply_strips_markdown_markers():
    bridge = load_bridge()
    details = {"metadata": {"channel": "wecom"}}
    reply = """今天（**2026年7月17日**）德国法兰克福天气：
• **当前：**多云，约 **20°C**
• **全天：**约 **18–28°C**
外出建议携带雨伞，穿透气夏装，并留意临时雷雨。
MEDIA: https://hydwang.xyz/media/weather"""

    out = bridge.deliver_wecom_response_url_reply(reply, details)

    assert "**" not in out
    assert "• 当前：多云，约 20°C" in out
    assert out.endswith("MEDIA: https://hydwang.xyz/media/weather")


def test_wecom_response_url_reply_drops_pipe_table_source():
    bridge = load_bridge()
    details = {"metadata": {"channel": "wecom"}}

    out = bridge.deliver_wecom_response_url_reply(
        "| Time | Weather |\n| --- | --- |\n| 7 am | Cloudy |\n适合穿薄外套\n"
        "MEDIA: https://h/weather",
        details,
    )

    assert "Time" not in out
    assert out == "适合穿薄外套\nMEDIA: https://h/weather"


def test_wecom_response_url_reply_limits_media_to_four():
    bridge = load_bridge()
    details = {"metadata": {"channel": "wecom"}}
    reply = "\n".join(f"MEDIA: https://h/{index}" for index in range(6))

    out = bridge.deliver_wecom_response_url_reply(reply, details)

    assert out.count("MEDIA:") == 4
    assert "https://h/3" in out
    assert "https://h/4" not in out


def test_wecom_response_url_reply_skips_other_channels():
    bridge = load_bridge()
    reply = "MEDIA: https://hydwang.xyz/media/image-3"

    assert bridge.deliver_wecom_response_url_reply(
        reply, {"metadata": {"channel": "wechat"}}
    ) == reply
