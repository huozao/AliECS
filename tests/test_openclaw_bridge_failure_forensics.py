"""What a failed turn's Feishu card is able to tell you.

2026-09-09 produced a card that named neither the device nor the fact that two
devices had been tried: webdock2 failed with UPLOAD_FAILED, the bridge re-sent
the turn to webdock1, and the card reported only webdock1's BROWSER_NOT_STARTED.
Investigation therefore started on the box that had merely inherited the
problem. These tests pin the three pieces that were missing.
"""
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


def test_device_comes_from_the_error_body_when_the_footer_is_absent():
    """webdock_footer is only written on the success path, so a failure card that
    relies on it carries no device at all — exactly the case that needs one."""
    bridge = load_bridge()
    message = bridge.diagnostic_message(
        "bridge -> WebDock 已联通；WebDock 返回 HTTP 500",
        "WebDock API",
        error_code="BROWSER_NOT_STARTED",
        device="webdock1",
        details={},
    )
    assert "设备 webdock1" in message


def test_footer_device_still_wins_when_no_explicit_device_is_given():
    bridge = load_bridge()
    message = bridge.diagnostic_message(
        "reason", "stop", details={"webdock_footer": {"device": "webdock2"}}
    )
    assert "设备 webdock2" in message


def test_cross_device_retry_is_reported_as_a_chain():
    bridge = load_bridge()
    message = bridge.diagnostic_message(
        "reason",
        "stop",
        error_code="BROWSER_NOT_STARTED",
        device="webdock1",
        attempts=[
            {"route": "primary", "device": "webdock2", "error_code": "UPLOAD_FAILED"},
            {"route": "standby", "device": "webdock1", "error_code": "BROWSER_NOT_STARTED"},
        ],
    )
    assert "尝试链：webdock2:UPLOAD_FAILED → webdock1:BROWSER_NOT_STARTED" in message


def test_single_attempt_adds_no_chain_line():
    """One device, one failure — a "chain" of one is noise on every ordinary card."""
    bridge = load_bridge()
    message = bridge.diagnostic_message(
        "reason",
        "stop",
        attempts=[{"route": "primary", "device": "webdock2", "error_code": "UPLOAD_FAILED"}],
    )
    assert "尝试链" not in message


def test_screenshot_url_becomes_a_media_marker():
    """The ordinary card path already turns MEDIA: markers into card images, so the
    screenshot rides that instead of a second delivery mechanism."""
    bridge = load_bridge()
    out = bridge.with_debug_screenshot(
        "ChatGPT 浏览器暂不可用", {"debug_screenshot_url": "http://172.17.0.1:11800/media/abc"}
    )
    assert out.endswith("\nMEDIA: http://172.17.0.1:11800/media/abc")
    assert bridge.split_ordered_segments(out)[-1] == (
        "image",
        "http://172.17.0.1:11800/media/abc",
    )


def test_missing_screenshot_leaves_the_reply_untouched():
    bridge = load_bridge()
    assert bridge.with_debug_screenshot("text", {}) == "text"
    assert bridge.with_debug_screenshot("text", {"debug_screenshot_url": "  "}) == "text"


def test_attempt_descriptor_defaults_to_a_named_error_code():
    """A job that failed without a structured error still has to say which device
    it was on; an empty code would render as "webdock2:" and read like truncation."""
    bridge = load_bridge()
    assert bridge._describe_attempt("primary", "webdock2", {}) == {
        "route": "primary",
        "device": "webdock2",
        "error_code": "UNKNOWN_ERROR",
    }
