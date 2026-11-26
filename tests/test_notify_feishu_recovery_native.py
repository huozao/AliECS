"""wechat-digest 飞书原生恢复卡渲染与开关回退离线测试。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = WORKTREE_ROOT / "services" / "backend-api"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.notify import dispatch  # noqa: E402
from app.notify.channels import feishu, wecom  # noqa: E402
from app.notify.models import (  # noqa: E402
    Notification,
    NotifyButton,
    NotifyChannelPayload,
    NotifyDecision,
)


def _sample_recovery_notification(*, with_extra_button: bool = False) -> Notification:
    buttons = (
        [NotifyButton(text="状态看板", url="https://notify.example.com/status", style="default")]
        if with_extra_button
        else []
    )
    return Notification(
        source="wechat-digest",
        event="wechat.login.retry",
        level="warn",
        title="微信登录二维码失效，需人工确认恢复",
        summary="点击按钮选择是否重新生成登录二维码。",
        buttons=buttons,
        decision=NotifyDecision(
            key="wechat-login:outage-20261126:2",
            question="是否重新生成微信登录二维码？",
            options=[
                {"key": "regen", "label": "重新生成二维码", "style": "primary"},
                {"key": "skip", "label": "暂不处理", "style": "default"},
            ],
        ),
        dedup_key="wechat-login:outage-20261126:2:retry",
    )


class FakeConn:
    def cursor(self):
        return mock.MagicMock()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeHttpResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHttpResp":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _collect_card_buttons(card: dict[str, Any]) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    for elem in card["body"]["elements"]:
        if elem.get("tag") == "button":
            buttons.append(elem)
        elif elem.get("tag") == "column_set":
            for col in elem.get("columns") or []:
                for item in col.get("elements") or []:
                    if item.get("tag") == "button":
                        buttons.append(item)
    return buttons


class TestWechatRecoveryNativeCards(unittest.TestCase):
    def test_default_flag_off_keeps_legacy_open_url_buttons(self) -> None:
        notif = _sample_recovery_notification()
        notif.buttons.extend(
            [
                NotifyButton(
                    text="重新生成二维码",
                    url="https://notify.example.com/api/v1/notify/decisions/101?t=tok1&opt=regen",
                    style="primary",
                ),
                NotifyButton(
                    text="暂不处理",
                    url="https://notify.example.com/api/v1/notify/decisions/101?t=tok1&opt=skip",
                    style="default",
                ),
            ]
        )
        with mock.patch.dict("os.environ", {}, clear=False):
            # 确保未配置时默认关闭
            import os

            os.environ.pop("NOTIFY_WECHAT_RECOVERY_NATIVE_CARDS", None)
            self.assertFalse(feishu.is_wechat_recovery_native_cards_enabled())
            card = feishu.build_card(notif, {})

        btns = _collect_card_buttons(card)
        self.assertEqual(len(btns), 2)
        for btn in btns:
            self.assertEqual(btn["behaviors"][0]["type"], "open_url")
            self.assertIn("/api/v1/notify/decisions/101?t=tok1&opt=", btn["behaviors"][0]["default_url"])

    def test_flag_on_renders_native_callback_buttons_without_leaking_recipient_or_secrets(self) -> None:
        notif = _sample_recovery_notification(with_extra_button=True)
        notif.buttons.extend(
            [
                NotifyButton(
                    text="重新生成二维码",
                    url="https://notify.example.com/api/v1/notify/decisions/101?t=tok1&opt=regen",
                    style="primary",
                ),
                NotifyButton(
                    text="暂不处理",
                    url="https://notify.example.com/api/v1/notify/decisions/101?t=tok1&opt=skip",
                    style="default",
                ),
            ]
        )
        with mock.patch.dict("os.environ", {"NOTIFY_WECHAT_RECOVERY_NATIVE_CARDS": "1"}):
            card = feishu.build_card(notif, {})

        btns = _collect_card_buttons(card)
        # 2 个原生 callback 按钮 + 1 个非决策 extra open_url 按钮；不重复展示决定 URL 按钮
        self.assertEqual(len(btns), 3)
        self.assertEqual(btns[0]["text"]["content"], "重新生成二维码")
        self.assertEqual(btns[0]["behaviors"], [
            {
                "type": "callback",
                "value": {
                    "action": "wechat-recovery:choose wechat-login:outage-20261126:2 regen",
                    "oc": "ocf1",
                    "k": "quick",
                    "a": "wechat-recovery:regen",
                    "q": "/wechat_recovery choose wechat-login:outage-20261126:2 regen",
                    "c": {"t": "group"},
                },
            }
        ])
        self.assertEqual(btns[1]["text"]["content"], "暂不处理")
        self.assertEqual(
            btns[1]["behaviors"][0]["value"]["action"],
            "wechat-recovery:choose wechat-login:outage-20261126:2 skip",
        )
        self.assertEqual(btns[1]["behaviors"][0]["value"]["a"], "wechat-recovery:skip")
        self.assertEqual(
            btns[1]["behaviors"][0]["value"]["q"],
            "/wechat_recovery choose wechat-login:outage-20261126:2 skip",
        )
        self.assertEqual(btns[2]["behaviors"][0]["type"], "open_url")
        self.assertEqual(btns[2]["behaviors"][0]["default_url"], "https://notify.example.com/status")

        # 验证 callback payload 中不包含群 ID、open_id 或令牌
        for callback_btn in btns[:2]:
            payload_json = json.dumps(callback_btn["behaviors"][0]["value"], ensure_ascii=False)
            self.assertNotIn("oc_", payload_json)
            self.assertNotIn("ou_", payload_json)
            self.assertNotIn("tok1", payload_json)

        # 验证保留网页确认页旧链接回退注释
        notations = [
            e["content"]
            for e in card["body"]["elements"]
            if e.get("tag") == "markdown" and e.get("text_size") == "notation"
        ]
        self.assertTrue(
            any("https://notify.example.com/api/v1/notify/decisions/101?t=tok1" in n for n in notations)
        )

        # 验证从 outbox storable_payload 重建后仍渲染同一原生回调卡片
        restored = Notification.from_stored(notif.storable_payload())
        with mock.patch.dict("os.environ", {"NOTIFY_WECHAT_RECOVERY_NATIVE_CARDS": "true"}):
            restored_card = feishu.build_card(restored, {})
        self.assertEqual(card, restored_card)

    def test_scope_restricts_native_cards_to_real_wechat_digest_recovery_only(self) -> None:
        cases = [
            Notification(
                source="wechat-digest",
                event="wechat.login.test",
                title="测试通知",
                buttons=[
                    NotifyButton(
                        text="看到了",
                        url="https://notify.example.com/api/v1/notify/decisions/9?t=t9&opt=seen",
                    ),
                    NotifyButton(
                        text="没收到",
                        url="https://notify.example.com/api/v1/notify/decisions/9?t=t9&opt=missing",
                    ),
                ],
                decision=NotifyDecision(
                    key="wechat-login:test:1700000000",
                    question="是否收到通知？",
                    options=[
                        {"key": "seen", "label": "看到了", "style": "primary"},
                        {"key": "missing", "label": "没收到", "style": "default"},
                    ],
                ),
            ),
            Notification(
                source="other-service",
                event="wechat.login.retry",
                title="其他来源",
                buttons=[
                    NotifyButton(
                        text="重生成",
                        url="https://notify.example.com/api/v1/notify/decisions/10?t=t10&opt=regen",
                    ),
                    NotifyButton(
                        text="跳过",
                        url="https://notify.example.com/api/v1/notify/decisions/10?t=t10&opt=skip",
                    ),
                ],
                decision=NotifyDecision(
                    key="wechat-login:outage-1:1",
                    question="?",
                    options=[
                        {"key": "regen", "label": "重生成", "style": "primary"},
                        {"key": "skip", "label": "跳过", "style": "default"},
                    ],
                ),
            ),
            Notification(
                source="wechat-digest",
                event="wechat.other.alert",
                title="非登录恢复决策",
                buttons=[
                    NotifyButton(
                        text="确认",
                        url="https://notify.example.com/api/v1/notify/decisions/11?t=t11&opt=ok",
                    ),
                    NotifyButton(
                        text="取消",
                        url="https://notify.example.com/api/v1/notify/decisions/11?t=t11&opt=cancel",
                    ),
                ],
                decision=NotifyDecision(
                    key="digest-job:11",
                    question="?",
                    options=[
                        {"key": "ok", "label": "确认", "style": "primary"},
                        {"key": "cancel", "label": "取消", "style": "default"},
                    ],
                ),
            ),
        ]
        with mock.patch.dict("os.environ", {"NOTIFY_WECHAT_RECOVERY_NATIVE_CARDS": "1"}):
            for notif in cases:
                self.assertFalse(feishu.is_wechat_recovery_decision(notif))
                card = feishu.build_card(notif, {})
                btns = _collect_card_buttons(card)
                self.assertTrue(btns)
                for btn in btns:
                    self.assertEqual(btn["behaviors"][0]["type"], "open_url")

        with self.assertRaises(ValueError):
            feishu.build_wechat_recovery_callback_value("wechat-login:test:1", "regen")
        with self.assertRaises(ValueError):
            feishu.build_wechat_recovery_callback_value("wechat-login:out-1:1", "exec_shell")

    def test_dispatch_deliver_preserves_decision_db_wecom_and_native_feishu_switch(self) -> None:
        notif = _sample_recovery_notification()
        sent_requests: list[tuple[str, dict[str, Any]]] = []

        def fake_opener(req, timeout=None):  # noqa: ARG001
            url = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            sent_requests.append((url, body))
            if "tenant_access_token" in url:
                return _FakeHttpResp({"code": 0, "tenant_access_token": "mock-tenant-token", "expire": 7200})
            return _FakeHttpResp({"code": 0, "data": {"message_id": "om_mock_1"}})

        feishu._token_cache.clear()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "NOTIFY_WECHAT_RECOVERY_NATIVE_CARDS": "1",
                    "FEISHU_TEST_APP_ID": "cli_mock",
                    "FEISHU_TEST_APP_SECRET": "sec_mock",
                },
            ),
            mock.patch("app.notify.dispatch._conn", return_value=FakeConn()),
            mock.patch("app.notify.dispatch.store.enqueue", return_value=(501, True)),
            mock.patch("app.notify.dispatch.store.upsert_decision", return_value=(77, "pending")) as upsert_mock,
            mock.patch("app.notify.dispatch.store.get_public_base_url", return_value="https://notify.example.com"),
            mock.patch(
                "app.notify.dispatch.store.matching_routes",
                return_value=[
                    {
                        "channel": "feishu",
                        "target_json": {
                            "profile": "TEST",
                            "receive_id": "oc_example_group",
                            "receive_id_type": "chat_id",
                        },
                    }
                ],
            ),
            mock.patch("app.notify.dispatch.store.create_deliveries", return_value=[{"id": 9001, "channel": "feishu", "target_json": {"profile": "TEST", "receive_id": "oc_example_group", "receive_id_type": "chat_id"}}]),
            mock.patch("app.notify.dispatch.store.mark_sent") as mark_sent_mock,
            mock.patch("app.notify.dispatch.sender_for", return_value=lambda n, t: feishu.send(n, t, opener=fake_opener)),
        ):
            result = dispatch.deliver(notif)

        upsert_mock.assert_called_once()
        mark_sent_mock.assert_called_once()
        self.assertEqual(result["decision"]["id"], 77)
        msg_calls = [body for url, body in sent_requests if "/im/v1/messages" in url]
        self.assertEqual(len(msg_calls), 1)
        sent_card = json.loads(msg_calls[0]["content"])
        btns = _collect_card_buttons(sent_card)
        self.assertEqual([b["behaviors"][0]["type"] for b in btns], ["callback", "callback"])
        # 企微通道仍保有完整决策 URL 按钮
        wecom_md = wecom.render_markdown(notif)
        self.assertIn("https://notify.example.com/api/v1/notify/decisions/77?", wecom_md)

    def test_dispatch_deliver_switch_off_reverts_feishu_to_open_url(self) -> None:
        notif = _sample_recovery_notification()
        sent_requests: list[tuple[str, dict[str, Any]]] = []

        def fake_opener(req, timeout=None):  # noqa: ARG001
            url = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            sent_requests.append((url, body))
            if "tenant_access_token" in url:
                return _FakeHttpResp({"code": 0, "tenant_access_token": "mock-tenant-token", "expire": 7200})
            return _FakeHttpResp({"code": 0, "data": {"message_id": "om_mock_2"}})

        feishu._token_cache.clear()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "NOTIFY_WECHAT_RECOVERY_NATIVE_CARDS": "0",
                    "FEISHU_TEST_APP_ID": "cli_mock",
                    "FEISHU_TEST_APP_SECRET": "sec_mock",
                },
            ),
            mock.patch("app.notify.dispatch._conn", return_value=FakeConn()),
            mock.patch("app.notify.dispatch.store.enqueue", return_value=(502, True)),
            mock.patch("app.notify.dispatch.store.upsert_decision", return_value=(78, "pending")),
            mock.patch("app.notify.dispatch.store.get_public_base_url", return_value="https://notify.example.com"),
            mock.patch(
                "app.notify.dispatch.store.matching_routes",
                return_value=[
                    {
                        "channel": "feishu",
                        "target_json": {
                            "profile": "TEST",
                            "receive_id": "oc_example_group",
                            "receive_id_type": "chat_id",
                        },
                    }
                ],
            ),
            mock.patch(
                "app.notify.dispatch.store.create_deliveries",
                return_value=[
                    {
                        "id": 9002,
                        "channel": "feishu",
                        "target_json": {
                            "profile": "TEST",
                            "receive_id": "oc_example_group",
                            "receive_id_type": "chat_id",
                        },
                    }
                ],
            ),
            mock.patch("app.notify.dispatch.store.mark_sent"),
            mock.patch("app.notify.dispatch.sender_for", return_value=lambda n, t: feishu.send(n, t, opener=fake_opener)),
        ):
            result = dispatch.deliver(notif)

        self.assertEqual(result["sent"], 1)
        msg_calls = [body for url, body in sent_requests if "/im/v1/messages" in url]
        self.assertEqual(len(msg_calls), 1)
        sent_card = json.loads(msg_calls[0]["content"])
        btns = _collect_card_buttons(sent_card)
        self.assertEqual([b["behaviors"][0]["type"] for b in btns], ["open_url", "open_url"])

    def test_attach_decision_buttons_to_native_respects_flag_and_backward_compat(self) -> None:
        native_card = {
            "schema": "2.0",
            "body": {"elements": [{"tag": "markdown", "content": "hi"}]},
        }
        payloads = {
            "feishu": NotifyChannelPayload(msg_type="interactive", content=dict(native_card)),
        }
        decision_buttons = [
            NotifyButton(text="重新生成二维码", url="https://notify.example.com/api/v1/notify/decisions/1?t=x&opt=regen", style="primary"),
            NotifyButton(text="暂不处理", url="https://notify.example.com/api/v1/notify/decisions/1?t=x&opt=skip", style="default"),
        ]
        # 原两参数签名默认保持 open_url
        dispatch.attach_decision_buttons_to_native(payloads, decision_buttons)
        btns = _collect_card_buttons(payloads["feishu"].content)
        self.assertEqual([b["behaviors"][0]["type"] for b in btns], ["open_url", "open_url"])

        # 传入原生回调元素后写入 callback 按钮
        notif = _sample_recovery_notification()
        payloads2 = {
            "feishu": NotifyChannelPayload(msg_type="interactive", content={"schema": "2.0", "body": {"elements": []}}),
        }
        with mock.patch.dict("os.environ", {"NOTIFY_WECHAT_RECOVERY_NATIVE_CARDS": "1"}):
            dispatch.attach_decision_buttons_to_native(
                payloads2,
                decision_buttons,
                native_button_elements=feishu.native_decision_button_elements(notif),
            )
        btns2 = _collect_card_buttons(payloads2["feishu"].content)
        self.assertEqual([b["behaviors"][0]["type"] for b in btns2], ["callback", "callback"])

    def test_example_test_card_fixture_matches_schema_without_real_secrets(self) -> None:
        fixture_path = WORKTREE_ROOT / "deploy" / "openclaw-feishu-recovery" / "test-card.example.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(data["card"]["schema"], "2.0")
        btns = _collect_card_buttons(data["card"])
        self.assertEqual(len(btns), 1)
        val = btns[0]["behaviors"][0]["value"]
        self.assertEqual(val["oc"], "ocf1")
        self.assertEqual(val["k"], "quick")
        self.assertEqual(val["q"], "/wechat_recovery test-card default")
        self.assertEqual(val["c"], {"t": "group"})


if __name__ == "__main__":
    unittest.main()
