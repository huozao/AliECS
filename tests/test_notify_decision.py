"""统一消息中枢：飞书卡片决策按钮测试。

覆盖接口契约（CONTRACT.md）与本次任务要求的关键特性：
1. 校验拒绝（选项数 2–5、重复 key、合法字符集、按钮总数不超过 5 超出 422 拒绝）
2. 同 key 幂等不清空选择（同 source_key + key 不新建决策、不清空已有选择）
3. GET 确认页不改状态（防止链接预取误触发）
4. POST 记录与改选历史（以最后一次为准，历史全部保留）
5. 错令牌/错决策 404（不区分不存在和令牌错）
6. 跨来源查询 404（不存在或属于其他来源一律 404）
7. 原生卡片插按钮（追加到 body.elements 末尾，格式错误显式报错）
8. 旧消息（无 decision）渲染不变
9. HTML 转义（防 XSS 注入，不加载任何外部资源）
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.notify import dispatch, store  # noqa: E402
from app.notify.channels import feishu  # noqa: E402
from app.notify.models import (  # noqa: E402
    Notification,
    NotifyButton,
    NotifyChannelPayload,
    NotifyDecision,
    NotifyDecisionOption,
    NotifyLink,
)
from app.routers import notify as notify_router  # noqa: E402

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()


def make_decision(**overrides: Any) -> NotifyDecision:
    data = {
        "key": "batch-20261108:swap-1",
        "question": "是否执行日界换装？",
        "options": [
            {"key": "keep", "label": "保留", "style": "primary"},
            {"key": "rollback", "label": "回退", "style": "danger"},
        ],
    }
    data.update(overrides)
    return NotifyDecision.model_validate(data)


def make_notification(**overrides: Any) -> Notification:
    payload: dict[str, Any] = {
        "source": "gold-spread-monitor",
        "event": "daily_decision",
        "level": "warn",
        "title": "换装决策确认",
        "summary": "请确认换装参数",
        "segments": [
            {"kind": "fields", "fields": [{"name": "合约", "value": "AU2612"}]},
        ],
        "dedup_key": "gold:decision:evt-1",
    }
    payload.update(overrides)
    return Notification.model_validate(payload)


class DecisionModelValidationTests(unittest.TestCase):
    def test_option_count_boundary(self) -> None:
        # 少于 2 个拒绝
        with self.assertRaises(ValueError):
            make_decision(options=[{"key": "only_one", "label": "唯一"}])
        # 2 个合法
        d2 = make_decision(options=[
            {"key": "a", "label": "A"},
            {"key": "b", "label": "B"},
        ])
        self.assertEqual(len(d2.options), 2)
        # 5 个合法
        d5 = make_decision(options=[
            {"key": f"k{i}", "label": f"L{i}"} for i in range(5)
        ])
        self.assertEqual(len(d5.options), 5)
        # 超出 5 个拒绝
        with self.assertRaises(ValueError):
            make_decision(options=[
                {"key": f"k{i}", "label": f"L{i}"} for i in range(6)
            ])

    def test_option_key_uniqueness(self) -> None:
        # 同一 decision 内选项 key 必须唯一
        with self.assertRaises(ValueError):
            make_decision(options=[
                {"key": "dup", "label": "选项1"},
                {"key": "dup", "label": "选项2"},
            ])

    def test_option_key_pattern(self) -> None:
        # option.key 必须匹配 [a-z0-9_-]{1,32}
        with self.assertRaises(ValueError):
            make_decision(options=[
                {"key": "UPPERCASE", "label": "大写拒绝"},
                {"key": "valid", "label": "正常"},
            ])
        with self.assertRaises(ValueError):
            make_decision(options=[
                {"key": "has space", "label": "空格拒绝"},
                {"key": "valid", "label": "正常"},
            ])

    def test_decision_key_pattern(self) -> None:
        # key 必须匹配 [A-Za-z0-9_.:-]{1,128}
        with self.assertRaises(ValueError):
            make_decision(key="bad/slash")
        valid_key = "batch-20261102-claude-db61d5f9:redis-swap-20261109"
        d = make_decision(key=valid_key)
        self.assertEqual(d.key, valid_key)

    def test_total_buttons_exceeding_five_is_rejected(self) -> None:
        # 契约：decision 与 buttons/link 合计按钮数不超过 5，超出时在入口用 422 拒绝，不悄悄截断
        # 3 个现有 buttons + 3 个 decision options = 6 个 -> 拒绝
        existing_buttons = [
            NotifyButton(text=f"B{i}", url=f"https://example.com/{i}") for i in range(3)
        ]
        decision = make_decision(options=[
            {"key": "a", "label": "A"},
            {"key": "b", "label": "B"},
            {"key": "c", "label": "C"},
        ])
        with self.assertRaises(ValueError) as ctx:
            make_notification(buttons=existing_buttons, decision=decision)
        self.assertIn("exceeds limit of 5", str(ctx.exception))

        # 4 个现有 buttons + 2 个 decision options = 6 个 -> 拒绝
        existing_buttons_4 = [
            NotifyButton(text=f"B{i}", url=f"https://example.com/{i}") for i in range(4)
        ]
        decision_2 = make_decision(options=[
            {"key": "a", "label": "A"},
            {"key": "b", "label": "B"},
        ])
        with self.assertRaises(ValueError):
            make_notification(buttons=existing_buttons_4, decision=decision_2)

        # 2 个 buttons + 1 个 link + 3 个 decision options = 6 个 -> 拒绝
        with self.assertRaises(ValueError):
            make_notification(
                buttons=[NotifyButton(text="B0", url="https://example.com/0"), NotifyButton(text="B1", url="https://example.com/1")],
                link=NotifyLink(text="L", url="https://example.com/link"),
                decision=decision,
            )

    def test_total_buttons_at_boundary_succeeds(self) -> None:
        # 3 个 buttons + 2 个 decision options = 5 个 -> 合法
        buttons = [
            NotifyButton(text=f"B{i}", url=f"https://example.com/{i}") for i in range(3)
        ]
        decision = make_decision(options=[
            {"key": "a", "label": "A"},
            {"key": "b", "label": "B"},
        ])
        n = make_notification(buttons=buttons, decision=decision)
        self.assertEqual(len(n.buttons), 3)
        self.assertEqual(len(n.decision.options), 2)


class DecisionNativePayloadTests(unittest.TestCase):
    def test_native_card_inserts_decision_buttons(self) -> None:
        native_card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "原生卡片测试"}},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "正文内容"}
                ]
            },
        }
        notification = make_notification(
            decision=make_decision(),
            channel_payloads={
                "feishu": NotifyChannelPayload(msg_type="interactive", content=native_card)
            },
        )
        decision_buttons = [
            NotifyButton(text="保留", url="https://hydwang.xyz/api/v1/notify/decisions/1?t=abc&opt=keep", style="primary"),
            NotifyButton(text="回退", url="https://hydwang.xyz/api/v1/notify/decisions/1?t=abc&opt=rollback", style="danger"),
        ]
        dispatch.attach_decision_buttons_to_native(notification.channel_payloads, decision_buttons)

        feishu_payload = notification.channel_payloads["feishu"]
        elements = feishu_payload.content["body"]["elements"]
        self.assertEqual(len(elements), 2)
        button_col_set = elements[1]
        self.assertEqual(button_col_set["tag"], "column_set")
        self.assertEqual(len(button_col_set["columns"]), 2)
        btn1 = button_col_set["columns"][0]["elements"][0]
        self.assertEqual(btn1["text"]["content"], "保留")
        self.assertEqual(btn1["behaviors"][0]["default_url"], decision_buttons[0].url)

    def test_native_card_string_content_inserts_buttons(self) -> None:
        native_card_str = json.dumps({
            "schema": "2.0",
            "body": {"elements": [{"tag": "markdown", "content": "str content"}]},
        })
        notification = make_notification(
            decision=make_decision(),
            channel_payloads={
                "feishu": NotifyChannelPayload(msg_type="interactive", content=native_card_str)
            },
        )
        decision_buttons = [
            NotifyButton(text="保留", url="https://hydwang.xyz/opt1"),
            NotifyButton(text="回退", url="https://hydwang.xyz/opt2"),
        ]
        dispatch.attach_decision_buttons_to_native(notification.channel_payloads, decision_buttons)

        feishu_payload = notification.channel_payloads["feishu"]
        self.assertIsInstance(feishu_payload.content, str)
        parsed = json.loads(feishu_payload.content)
        self.assertEqual(len(parsed["body"]["elements"]), 2)

    def test_native_card_unsupported_structure_fails_explicitly(self) -> None:
        # 原生载荷结构没有 body.elements 时显式报错拒绝，不静默丢弃按钮
        bad_card = {"schema": "2.0", "wrong_body": {}}
        payloads = {
            "feishu": NotifyChannelPayload(msg_type="interactive", content=bad_card)
        }
        with self.assertRaises(ValueError) as ctx:
            dispatch.attach_decision_buttons_to_native(
                payloads,
                [NotifyButton(text="A", url="https://example.com/a")]
            )
        self.assertIn("missing 'body.elements'", str(ctx.exception))

    def test_unsupported_channel_fails_explicitly(self) -> None:
        payloads = {
            "unknown_ch": NotifyChannelPayload(msg_type="custom", content={"data": 123})
        }
        with self.assertRaises(ValueError) as ctx:
            dispatch.attach_decision_buttons_to_native(
                payloads,
                [NotifyButton(text="A", url="https://example.com/a")]
            )
        self.assertIn("unsupported channel", str(ctx.exception))


class DecisionRenderingTests(unittest.TestCase):
    def test_old_notification_without_decision_render_unchanged(self) -> None:
        n = make_notification(decision=None)
        card = feishu.build_card(n, {})
        # 没有 decision 时，elements 不包含决策按钮，只有正文字段和 notation
        tags = [e.get("tag") for e in card["body"]["elements"]]
        self.assertNotIn("button", tags)
        self.assertEqual(len(n.all_buttons()), 0)

    def test_html_escaping_in_confirmation_and_result_page(self) -> None:
        malicious_decision = {
            "id": 1,
            "source_key": "gold-spread-monitor",
            "decision_key": "xss-test",
            "token_sha256": "fake",
            "question": "<script>alert('xss')</script> & <b>question</b>",
            "options": [
                {"key": "opt1", "label": "<img src=x onerror=alert(1)>", "style": "primary"},
                {"key": "opt2", "label": "\"quoted\" & 'single'", "style": "default"},
            ],
            "status": "pending",
            "chosen_option": None,
            "chosen_label": None,
            "chosen_at": None,
        }
        # GET 确认页渲染
        get_html = notify_router._render_decision_page(malicious_decision, token="tok123", selected_opt="opt1")
        # 确认不含未转义的恶意 script 或 img 标签
        self.assertNotIn("<script>", get_html)
        self.assertNotIn("<img src=x", get_html)
        # 确认包含转义字符
        self.assertIn("&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;", get_html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", get_html)
        # 确认不加载外部样式表或脚本资源
        self.assertNotIn("<link rel=\"stylesheet\"", get_html)
        self.assertNotIn("<script src=", get_html)

        # POST 结果页渲染
        post_html = notify_router._render_decision_result_page(
            malicious_decision,
            option_label="<img src=x onerror=alert(1)>",
            chosen_at=datetime(2026, 11, 8, 6, 30, tzinfo=timezone.utc),
        )
        self.assertNotIn("<script>", post_html)
        self.assertNotIn("<img src=x", post_html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", post_html)
        self.assertNotIn("<link rel=\"stylesheet\"", post_html)
        self.assertNotIn("<script src=", post_html)


class StateMockDb:
    """提供支持完整状态流转的数据库模拟，真实检验幂等、GET只读、POST记录与改选历史。"""

    def __init__(self) -> None:
        self.decisions: dict[int, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        # 令牌表：(decision_id, token_sha256)。测试直接预置的决策可在字典里带 token_sha256，
        # 读取时与本表合并，等价于该决策发过一次卡。
        self.tokens: list[tuple[int, str]] = []
        self.sources: dict[str, str] = {
            "gold-spread-monitor": hashlib.sha256(b"correct_token").hexdigest(),
            "other-source": hashlib.sha256(b"other_token").hexdigest(),
        }
        self.outbox: list[dict[str, Any]] = []
        self.deliveries: list[dict[str, Any]] = []
        self.routes: list[dict[str, Any]] = [
            {
                "id": 1,
                "source_key": "*",
                "event_pattern": "*",
                "min_level": "info",
                "channel": "feishu",
                "target_json": {"profile": "COMPANY_A", "receive_id": "oc_test"},
                "sort_order": 100,
            }
        ]
        self.next_decision_id = 1
        self.next_event_id = 1
        self.next_outbox_id = 1
        self.next_delivery_id = 1
        self.executed_sqls: list[tuple[str, Any]] = []
        self.commits = 0

    def cursor(self) -> "StateMockCursor":
        return StateMockCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        return None


class StateMockCursor:
    def __init__(self, db: StateMockDb) -> None:
        self.db = db
        self._last_row: Any = None
        self._last_rowset: list[Any] = []

    def __enter__(self) -> "StateMockCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        norm = " ".join(sql.split())
        self.db.executed_sqls.append((norm, params))

        if "SELECT token_sha256 FROM notify_sources WHERE source_key = %s" in norm:
            source_key = params[0]
            if source_key in self.db.sources:
                self._last_row = (self.db.sources[source_key],)
            else:
                self._last_row = None

        elif "SELECT id, status FROM notify_decisions WHERE source_key = %s AND decision_key = %s" in norm:
            source_key, dec_key = params[0], params[1]
            found = None
            for d in self.db.decisions.values():
                if d["source_key"] == source_key and d["decision_key"] == dec_key:
                    found = (d["id"], d["status"])
                    break
            self._last_row = found

        elif "INSERT INTO notify_decisions" in norm:
            d_id = self.db.next_decision_id
            self.db.next_decision_id += 1
            now = datetime.now(timezone.utc)
            self.db.decisions[d_id] = {
                "id": d_id,
                "source_key": params[0],
                "decision_key": params[1],
                "question": params[2],
                "options_json": params[3],
                "status": "pending",
                "chosen_option": None,
                "chosen_label": None,
                "chosen_at": None,
                "created_at": now,
                "updated_at": now,
            }
            self._last_row = (d_id,)

        elif "INSERT INTO notify_decision_tokens" in norm:
            self.db.tokens.append((int(params[0]), params[1]))

        elif "SELECT token_sha256 FROM notify_decision_tokens WHERE decision_id = %s" in norm:
            d_id = int(params[0])
            rows = [(h,) for (dec, h) in self.db.tokens if dec == d_id]
            seeded = self.db.decisions.get(d_id, {}).get("token_sha256")
            if seeded:
                rows.insert(0, (seeded,))
            self._last_rowset = rows

        elif "SELECT id, source_key, decision_key, question, options_json, status, chosen_option, chosen_label, chosen_at, created_at, updated_at FROM notify_decisions WHERE id = %s" in norm:
            d_id = int(params[0])
            d = self.db.decisions.get(d_id)
            if d:
                self._last_row = (
                    d["id"], d["source_key"], d["decision_key"],
                    d["question"], d["options_json"], d["status"], d["chosen_option"],
                    d["chosen_label"], d["chosen_at"], d["created_at"], d["updated_at"],
                )
            else:
                self._last_row = None

        elif "INSERT INTO notify_decision_events" in norm:
            ev_id = self.db.next_event_id
            self.db.next_event_id += 1
            now = datetime.now(timezone.utc)
            self.db.events.append({
                "id": ev_id,
                "decision_id": int(params[0]),
                "option_key": params[1],
                "option_label": params[2],
                "client_ip": params[3],
                "user_agent": params[4],
                "created_at": now,
            })
            self._last_row = (now,)

        elif "UPDATE notify_decisions SET status = 'answered'" in norm:
            opt_key, opt_label, chosen_at, d_id = params[0], params[1], params[2], params[3]
            if d_id in self.db.decisions:
                d = self.db.decisions[d_id]
                d["status"] = "answered"
                d["chosen_option"] = opt_key
                d["chosen_label"] = opt_label
                d["chosen_at"] = chosen_at
                d["updated_at"] = datetime.now(timezone.utc)

        elif "SELECT id, source_key, decision_key, question, options_json, status, chosen_option, chosen_label, chosen_at, created_at, updated_at FROM notify_decisions WHERE source_key = %s AND decision_key = %s" in norm:
            source_key, dec_key = params[0], params[1]
            found = None
            for d in self.db.decisions.values():
                if d["source_key"] == source_key and d["decision_key"] == dec_key:
                    found = (
                        d["id"], d["source_key"], d["decision_key"],
                        d["question"], d["options_json"], d["status"], d["chosen_option"],
                        d["chosen_label"], d["chosen_at"], d["created_at"], d["updated_at"],
                    )
                    break
            self._last_row = found

        elif "SELECT option_key, created_at FROM notify_decision_events WHERE decision_id = %s" in norm:
            d_id = int(params[0])
            matched = [
                (ev["option_key"], ev["created_at"])
                for ev in self.db.events
                if ev["decision_id"] == d_id
            ]
            self._last_rowset = matched

        elif "FROM notify_decisions WHERE source_key = %s" in norm:
            source_key = params[0]
            status_filter = params[1] if len(params) == 3 else None
            limit = params[-1]
            rows = []
            for d in self.db.decisions.values():
                if d["source_key"] != source_key:
                    continue
                if status_filter and d["status"] != status_filter:
                    continue
                rows.append((
                    d["id"], d["decision_key"], d["question"], d["options_json"],
                    d["status"], d["chosen_option"], d["chosen_label"], d["chosen_at"],
                    d["created_at"],
                ))
            rows.sort(key=lambda x: x[8], reverse=True)
            self._last_rowset = rows[:limit]

        elif "INSERT INTO notify_outbox" in norm:
            out_id = self.db.next_outbox_id
            self.db.next_outbox_id += 1
            self.db.outbox.append({"id": out_id, "dedup_key": params[0]})
            self._last_row = (out_id,)

        elif "SELECT id, source_key, event_pattern, min_level, channel, target_json, sort_order FROM notify_routes" in norm:
            self._last_rowset = [
                (r["id"], r["source_key"], r["event_pattern"], r["min_level"], r["channel"], r["target_json"], r["sort_order"])
                for r in self.db.routes
            ]

        elif "INSERT INTO notify_deliveries" in norm:
            deliv_id = self.db.next_delivery_id
            self.db.next_delivery_id += 1
            self.db.deliveries.append({"id": deliv_id, "status": "pending"})
            self._last_row = (deliv_id,)

        elif "UPDATE notify_deliveries SET status = 'sent'" in norm:
            d_id = int(params[0])
            for d in self.db.deliveries:
                if d["id"] == d_id:
                    d["status"] = "sent"

    def fetchone(self) -> Any:
        return self._last_row

    def fetchall(self) -> Any:
        return self._last_rowset


class DecisionApiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = StateMockDb()
        self.app = FastAPI()
        self.app.include_router(notify_router.router)
        self.client = TestClient(self.app)

    def test_send_idempotent_same_key_preserves_decision_and_choices(self) -> None:
        # 1. 首次投递带决策的消息
        notification1 = make_notification(
            decision=make_decision(key="swap-key-1"),
            dedup_key="evt-msg-1",
        )
        sent_notifications: list[Notification] = []
        with mock.patch.object(notify_router, "_conn", return_value=self.db), \
             mock.patch.object(dispatch, "_conn", return_value=self.db), \
             mock.patch.object(dispatch, "sender_for", return_value=lambda n, t: sent_notifications.append(n)):
            headers = {
                "X-Notify-Source": "gold-spread-monitor",
                "X-Notify-Token": "correct_token",
            }
            resp1 = self.client.post("/v1/internal/notify/send", json=notification1.model_dump(mode="json"), headers=headers)

        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertIn("decision", data1)
        dec_id = data1["decision"]["id"]
        self.assertEqual(data1["decision"]["key"], "swap-key-1")
        self.assertEqual(data1["decision"]["status"], "pending")

        # 从卡片按钮或库中获取生成的 token
        self.assertEqual(len(sent_notifications), 1)
        buttons = sent_notifications[0].buttons
        decision_button = next(b for b in buttons if b.text == "保留")
        m = re.search(r"[?&]t=([^&]+)", decision_button.url)
        self.assertIsNotNone(m)
        token = m.group(1)

        # 2. 模拟用户选择 POST：选择 "keep"
        with mock.patch.object(notify_router, "_conn", return_value=self.db):
            post_resp = self.client.post(
                f"/v1/notify/decisions/{dec_id}",
                data={"t": token, "opt": "keep"},
            )
        self.assertEqual(post_resp.status_code, 200)
        self.assertIn("已记录：保留", post_resp.text)
        self.assertEqual(self.db.decisions[dec_id]["status"], "answered")
        self.assertEqual(self.db.decisions[dec_id]["chosen_option"], "keep")

        # 3. 再次发送相同 decision.key（即使不同 dedup_key 或重试）
        notification2 = make_notification(
            decision=make_decision(key="swap-key-1"),
            dedup_key="evt-msg-2",
        )
        with mock.patch.object(notify_router, "_conn", return_value=self.db), \
             mock.patch.object(dispatch, "_conn", return_value=self.db), \
             mock.patch.object(dispatch, "sender_for", return_value=lambda n, t: None):
            resp2 = self.client.post("/v1/internal/notify/send", json=notification2.model_dump(mode="json"), headers=headers)

        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        # 验证决策 ID 没有变，状态仍然是 answered，未被清空
        self.assertEqual(data2["decision"]["id"], dec_id)
        self.assertEqual(data2["decision"]["status"], "answered")
        self.assertEqual(self.db.decisions[dec_id]["chosen_option"], "keep")
        self.assertEqual(len(self.db.events), 1)

    def test_get_decision_page_does_not_modify_state(self) -> None:
        # 准备一个 pending 的决策
        now = datetime.now(timezone.utc)
        token = "test_token_secret_123"
        self.db.decisions[10] = {
            "id": 10,
            "source_key": "gold-spread-monitor",
            "decision_key": "pending-key",
            "token_sha256": store.token_digest(token),
            "question": "是否保留？",
            "options_json": [
                {"key": "yes", "label": "是", "style": "primary"},
                {"key": "no", "label": "否", "style": "danger"},
            ],
            "status": "pending",
            "chosen_option": None,
            "chosen_label": None,
            "chosen_at": None,
            "created_at": now,
            "updated_at": now,
        }

        executed_before = len(self.db.executed_sqls)
        with mock.patch.object(notify_router, "_conn", return_value=self.db):
            resp = self.client.get(f"/v1/notify/decisions/10?t={token}&opt=yes")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("决策确认", resp.text)
        self.assertIn("当前状态：<span>未选</span>", resp.text)
        self.assertIn("确认选择：是", resp.text)

        # 断言状态未被改变
        self.assertEqual(self.db.decisions[10]["status"], "pending")
        self.assertIsNone(self.db.decisions[10]["chosen_option"])
        self.assertEqual(len(self.db.events), 0)

        # 断言没有执行任何 UPDATE 或 INSERT
        new_sqls = self.db.executed_sqls[executed_before:]
        for sql, _ in new_sqls:
            self.assertFalse(sql.startswith("UPDATE"))
            self.assertFalse(sql.startswith("INSERT"))

    def test_post_records_choice_and_preserves_revision_history(self) -> None:
        now = datetime.now(timezone.utc)
        token = "tok_change_choice"
        self.db.decisions[20] = {
            "id": 20,
            "source_key": "gold-spread-monitor",
            "decision_key": "choice-key",
            "token_sha256": store.token_digest(token),
            "question": "外发保留还是回退？",
            "options_json": [
                {"key": "keep", "label": "保留", "style": "primary"},
                {"key": "rollback", "label": "回退", "style": "danger"},
            ],
            "status": "pending",
            "chosen_option": None,
            "chosen_label": None,
            "chosen_at": None,
            "created_at": now,
            "updated_at": now,
        }

        # 1. 第一次选择 keep
        with mock.patch.object(notify_router, "_conn", return_value=self.db):
            resp1 = self.client.post(
                "/v1/notify/decisions/20",
                data={"t": token, "opt": "keep"},
            )
        self.assertEqual(resp1.status_code, 200)
        self.assertIn("已记录：保留", resp1.text)
        self.assertEqual(self.db.decisions[20]["status"], "answered")
        self.assertEqual(self.db.decisions[20]["chosen_option"], "keep")
        self.assertEqual(len(self.db.events), 1)

        # 2. 第二次改选 rollback
        with mock.patch.object(notify_router, "_conn", return_value=self.db):
            resp2 = self.client.post(
                "/v1/notify/decisions/20",
                data={"t": token, "opt": "rollback"},
            )
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("已记录：回退", resp2.text)
        # 以最后一次为准
        self.assertEqual(self.db.decisions[20]["status"], "answered")
        self.assertEqual(self.db.decisions[20]["chosen_option"], "rollback")
        self.assertEqual(self.db.decisions[20]["chosen_label"], "回退")
        # 历史全部保留
        self.assertEqual(len(self.db.events), 2)
        self.assertEqual(self.db.events[0]["option_key"], "keep")
        self.assertEqual(self.db.events[1]["option_key"], "rollback")

        # 3. 生产者内部查询 by-key 验证
        headers = {
            "X-Notify-Source": "gold-spread-monitor",
            "X-Notify-Token": "correct_token",
        }
        with mock.patch.object(notify_router, "_conn", return_value=self.db):
            detail_resp = self.client.get(
                "/v1/internal/notify/decisions/by-key?key=choice-key",
                headers=headers,
            )
        self.assertEqual(detail_resp.status_code, 200)
        detail = detail_resp.json()
        self.assertEqual(detail["status"], "answered")
        self.assertEqual(detail["chosen_option"], "rollback")
        self.assertEqual(detail["chosen_label"], "回退")
        self.assertEqual(len(detail["history"]), 2)
        self.assertEqual(detail["history"][0]["option"], "keep")
        self.assertEqual(detail["history"][1]["option"], "rollback")
        # 验证带时区 ISO8601
        self.assertTrue(re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", detail["chosen_at"]))
        self.assertTrue(re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", detail["history"][0]["at"]))

    def test_invalid_token_or_missing_decision_returns_404(self) -> None:
        token = "valid_tok"
        self.db.decisions[30] = {
            "id": 30,
            "source_key": "gold-spread-monitor",
            "decision_key": "k30",
            "token_sha256": store.token_digest(token),
            "question": "Q",
            "options_json": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
            "status": "pending",
            "chosen_option": None,
            "chosen_label": None,
            "chosen_at": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        with mock.patch.object(notify_router, "_conn", return_value=self.db):
            # 不存在的 ID -> 404
            r1 = self.client.get("/v1/notify/decisions/99999?t=valid_tok")
            self.assertEqual(r1.status_code, 404)
            self.assertEqual(r1.json()["detail"], "decision not found")

            # 错令牌 -> 404
            r2 = self.client.get("/v1/notify/decisions/30?t=wrong_tok")
            self.assertEqual(r2.status_code, 404)
            self.assertEqual(r2.json()["detail"], "decision not found")

            # 缺失令牌 -> 404
            r3 = self.client.get("/v1/notify/decisions/30")
            self.assertEqual(r3.status_code, 404)

            # POST 错令牌 -> 404
            r4 = self.client.post("/v1/notify/decisions/30", data={"t": "wrong_tok", "opt": "a"})
            self.assertEqual(r4.status_code, 404)

            # POST 不存在的选项 -> 422
            r5 = self.client.post("/v1/notify/decisions/30", data={"t": token, "opt": "non_exist"})
            self.assertEqual(r5.status_code, 422)

    def test_cross_source_isolation_returns_404(self) -> None:
        # 决策属于 gold-spread-monitor
        self.db.decisions[40] = {
            "id": 40,
            "source_key": "gold-spread-monitor",
            "decision_key": "secret-gold-decision",
            "token_sha256": "any",
            "question": "敏感决策",
            "options_json": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
            "status": "pending",
            "chosen_option": None,
            "chosen_label": None,
            "chosen_at": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        # 用 other-source 的凭据查 gold-spread-monitor 的决策
        headers_other = {
            "X-Notify-Source": "other-source",
            "X-Notify-Token": "other_token",
        }
        with mock.patch.object(notify_router, "_conn", return_value=self.db):
            resp = self.client.get(
                "/v1/internal/notify/decisions/by-key?key=secret-gold-decision",
                headers=headers_other,
            )
        # 契约：不存在或属于其他来源时一律返回 404
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "decision not found")

        # 列表查询也隔离
        with mock.patch.object(notify_router, "_conn", return_value=self.db):
            list_resp = self.client.get(
                "/v1/internal/notify/decisions",
                headers=headers_other,
            )
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(len(list_resp.json()), 0)


if __name__ == "__main__":
    unittest.main()
