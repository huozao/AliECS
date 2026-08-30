"""统一消息中枢：消息模型、路由匹配、各通道渲染、投递编排与重试记账。"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 这几个 import 必须留在模块顶部（collect 阶段执行）。
# 同目录下的 worker 测试（test_managed_contacts_sync 等）会把 sys.modules['app']
# 换成 doc-sync-worker 的同名包，之后再做延迟 import 就会 ModuleNotFoundError。
from app.notify import dispatch, store  # noqa: E402
from app.notify.channels import feishu, wecom  # noqa: E402
from app.notify.models import Notification  # noqa: E402

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()


def make_notification(**overrides: Any) -> Notification:
    payload: dict[str, Any] = {
        "source": "gold-spread-monitor",
        "event": "wrong_price_detected",
        "level": "error",
        "title": "价差异常",
        "summary": "AU2612 偏离 1.24%",
        "segments": [
            {"kind": "fields", "fields": [{"name": "合约", "value": "AU2612"}]},
            {"kind": "text", "text": "## 详情\n持续 12 秒"},
        ],
        "dedup_key": "gold:wrong_price:evt-1",
    }
    payload.update(overrides)
    return Notification.model_validate(payload)


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((" ".join(sql.split()), params))

    def fetchone(self) -> Any:
        return self.conn.rows.pop(0) if self.conn.rows else None

    def fetchall(self) -> Any:
        return self.conn.rowsets.pop(0) if self.conn.rowsets else []


class FakeConnection:
    def __init__(self, rows: list[Any] | None = None, rowsets: list[Any] | None = None) -> None:
        self.rows = list(rows or [])
        self.rowsets = list(rowsets or [])
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        return None


class ModelTests(unittest.TestCase):
    def test_explicit_dedup_key_is_kept(self) -> None:
        self.assertEqual(make_notification().dedup_key, "gold:wrong_price:evt-1")

    def test_auto_dedup_key_is_stable_for_same_content(self) -> None:
        occurred = "2026-08-30T10:00:00+00:00"
        first = make_notification(dedup_key="", occurred_at=occurred)
        second = make_notification(dedup_key="", occurred_at=occurred)
        self.assertEqual(first.dedup_key, second.dedup_key)
        self.assertTrue(first.dedup_key.startswith("auto:gold-spread-monitor:"))

    def test_image_bytes_are_stripped_before_storage(self) -> None:
        notification = make_notification(
            segments=[{"kind": "image", "image_ref": "chart"}],
            images=[{"ref": "chart", "caption": "走势", "png_base64": PNG}],
        )
        payload = notification.storable_payload()
        self.assertNotIn("png_base64", json.dumps(payload))
        self.assertEqual(payload["images"][0]["base64_characters"], len(PNG))

    def test_restored_notification_has_no_images(self) -> None:
        """重试时图已丢失——这是 payload 不存 base64 的已知代价，必须留在测试里。"""
        notification = make_notification(
            segments=[{"kind": "text", "text": "正文"}, {"kind": "image", "image_ref": "chart"}],
            images=[{"ref": "chart", "png_base64": PNG}],
        )
        restored = Notification.from_stored(notification.storable_payload())
        self.assertEqual(restored.images, [])
        self.assertEqual([segment.kind for segment in restored.segments], ["text"])
        self.assertEqual(restored.title, "价差异常")

    def test_segment_referencing_unknown_image_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_notification(segments=[{"kind": "image", "image_ref": "missing"}])

    def test_plain_text_contains_every_segment(self) -> None:
        text = make_notification().plain_text()
        self.assertIn("价差异常", text)
        self.assertIn("合约：AU2612", text)
        self.assertIn("持续 12 秒", text)


class RouteMatchingTests(unittest.TestCase):
    ROUTES = [
        (1, "*", "*", "info", "feishu", {"receive_id": "all"}, 10),
        (2, "gold-spread-monitor", "wrong_price_*", "error", "wecom_bot", {"webhook_env": "G1"}, 20),
        (3, "doc-sync", "*", "warn", "wecom_app", {"profile": "COMPANY_A"}, 30),
    ]

    def _match(self, source: str, event: str, level: str) -> list[int]:
        conn = FakeConnection(rowsets=[list(self.ROUTES)])
        return [route["id"] for route in store.matching_routes(conn, source, event, level)]

    def test_wildcard_route_matches_everything(self) -> None:
        self.assertIn(1, self._match("anything", "any.event", "info"))

    def test_event_glob_and_source_must_both_match(self) -> None:
        self.assertEqual(self._match("gold-spread-monitor", "wrong_price_detected", "error"), [1, 2])
        self.assertEqual(self._match("gold-spread-monitor", "replay_summary", "error"), [1])

    def test_level_below_threshold_is_filtered_out(self) -> None:
        """warn 的消息不该进 min_level=error 的 target。"""
        self.assertEqual(self._match("gold-spread-monitor", "wrong_price_detected", "warn"), [1])

    def test_fatal_passes_every_threshold(self) -> None:
        self.assertEqual(self._match("doc-sync", "sync.failed", "fatal"), [1, 3])


class FeishuRenderTests(unittest.TestCase):
    def test_card_interleaves_text_and_images_in_order(self) -> None:
        notification = make_notification(
            segments=[
                {"kind": "text", "text": "开头"},
                {"kind": "image", "image_ref": "chart"},
                {"kind": "text", "text": "结尾"},
            ],
            images=[{"ref": "chart", "png_base64": PNG}],
        )
        card = feishu.build_card(notification, {"chart": "img_key_1"})
        kinds = [element.get("tag") for element in card["elements"]]
        # summary、开头、图、结尾、footer note
        self.assertEqual(kinds[:4], ["div", "div", "img", "div"])
        self.assertEqual(card["header"]["template"], "red")

    def test_missing_image_key_leaves_a_visible_note(self) -> None:
        """图传失败不能让卡片凭空少一块，否则读的人不知道本该有图。"""
        notification = make_notification(
            segments=[{"kind": "image", "image_ref": "chart"}],
            images=[{"ref": "chart", "png_base64": PNG}],
        )
        card = feishu.build_card(notification, {})
        notes = [
            element for element in card["elements"]
            if element.get("tag") == "note"
            and "失败" in json.dumps(element, ensure_ascii=False)
        ]
        self.assertEqual(len(notes), 1)

    def test_atx_heading_is_converted_to_bold(self) -> None:
        self.assertEqual(feishu._lark_md("## 详情"), "**详情**")

    def test_send_falls_back_to_text_when_card_is_rejected(self) -> None:
        """卡片被拒时退回纯文本：图丢了但字还在，与 gold_spread 现有降级同口径。"""
        calls: list[dict[str, Any]] = []

        def fake_opener(request, timeout=0):  # noqa: ANN001
            body = json.loads(request.data.decode("utf-8")) if request.data else {}
            url = request.full_url
            if "tenant_access_token" in url:
                return FakeResponse({"code": 0, "tenant_access_token": "t", "expire": 7200})
            calls.append(body)
            if body.get("msg_type") == "interactive":
                return FakeResponse({"code": 230001, "msg": "card rejected"})
            return FakeResponse({"code": 0})

        with mock.patch.dict("os.environ", {"FEISHU_COMPANY_A_APP_ID": "a", "FEISHU_COMPANY_A_APP_SECRET": "b"}):
            feishu._token_cache.clear()
            feishu.send(make_notification(), {"receive_id": "oc_1"}, opener=fake_opener)

        self.assertEqual([call["msg_type"] for call in calls], ["interactive", "text"])
        self.assertIn("价差异常", json.loads(calls[1]["content"])["text"])


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class WecomRenderTests(unittest.TestCase):
    def test_markdown_contains_fields_and_link(self) -> None:
        notification = make_notification(link={"text": "查看", "url": "https://hydwang.xyz/sync/"})
        rendered = wecom.render_markdown(notification)
        self.assertIn("**合约**：AU2612", rendered)
        self.assertIn("[查看](https://hydwang.xyz/sync/)", rendered)
        self.assertIn("> gold-spread-monitor · wrong_price_detected", rendered)

    def test_markdown_is_truncated_to_wecom_limit(self) -> None:
        notification = make_notification(segments=[{"kind": "text", "text": "长" * 5000}])
        rendered = wecom.render_markdown(notification)
        self.assertLessEqual(len(rendered.encode("utf-8")), wecom.MARKDOWN_MAX_BYTES)
        self.assertTrue(rendered.endswith("（已截断）"))

    def test_bot_sends_markdown_then_each_image(self) -> None:
        notification = make_notification(
            segments=[{"kind": "image", "image_ref": "chart"}],
            images=[{"ref": "chart", "png_base64": PNG}],
        )
        posted: list[dict[str, Any]] = []

        def fake_opener(request, timeout=0):  # noqa: ANN001
            posted.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse({"errcode": 0})

        with mock.patch.dict("os.environ", {"G1": "https://qyapi.weixin.qq.com/hook?key=x"}):
            wecom.send_bot(notification, {"webhook_env": "G1"}, opener=fake_opener)

        self.assertEqual([item["msgtype"] for item in posted], ["markdown", "image"])
        self.assertIn("md5", posted[1]["image"])

    def test_bot_without_webhook_env_fails_loudly(self) -> None:
        with self.assertRaises(RuntimeError):
            wecom.send_bot(make_notification(), {})

    def test_agent_id_accepts_the_historical_typo_key(self) -> None:
        """SOPS 里的 key 是 WECOM_COMPANY_A_gentId（少个 A），是历史 typo。"""
        with mock.patch.dict(
            "os.environ",
            {"WECOM_COMPANY_A_CORP_ID": "c", "WECOM_COMPANY_A_APP_SECRET": "s", "WECOM_COMPANY_A_gentId": "1000002"},
            clear=False,
        ):
            self.assertEqual(wecom.app_credentials("COMPANY_A")[2], "1000002")


class RetryAccountingTests(unittest.TestCase):
    def test_failure_schedules_backoff_and_keeps_pending(self) -> None:
        conn = FakeConnection(rows=[(0,)])
        self.assertEqual(store.mark_failed(conn, 7, "boom"), "pending")

    def test_last_attempt_is_marked_dead(self) -> None:
        """退避档位用完就判 dead，不能无限重试占着队列。"""
        conn = FakeConnection(rows=[(store.MAX_ATTEMPTS - 1,)])
        self.assertEqual(store.mark_failed(conn, 7, "boom"), "dead")


class DispatchTests(unittest.TestCase):
    def test_duplicate_dedup_key_is_not_delivered_twice(self) -> None:
        conn = FakeConnection(rows=[None, (42,)])  # INSERT 未返回 → 取已有行
        with mock.patch.object(dispatch, "sender_for") as sender:
            result = dispatch.deliver(make_notification(), conn=conn)
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["sent"], 0)
        sender.assert_not_called()

    def test_no_matching_route_reports_zero_targets(self) -> None:
        """没有路由命中时消息仍然落库，但调用方必须能看出没人收到。"""
        conn = FakeConnection(rows=[(43,)], rowsets=[[]])
        result = dispatch.deliver(make_notification(), conn=conn)
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["targets"], 0)


class CrossServiceContractTests(unittest.TestCase):
    """doc-sync-worker 写进 outbox 的 payload，必须能被 backend-api 解析回来。

    两者在不同镜像里（CI 的构建 context 是各自的 services/<name>），没有编译期检查，
    也没有共享包——这个契约只有这个测试守着。改任一侧的 payload 结构都要跑它。
    """

    @staticmethod
    def _load_worker_client():
        """按路径加载 worker 的 notify_client。

        不能直接 import：这个进程里 sys.modules['app'] 是 backend-api 的同名包。
        本模块已刻意做到模块级不依赖 app.storage，所以按路径加载是安全的。
        """
        worker_module = (
            Path(__file__).resolve().parents[1]
            / "services" / "doc-sync-worker" / "app" / "notify_client.py"
        )
        spec = importlib.util.spec_from_file_location("worker_notify_client", worker_module)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_worker_payload_is_parsable_by_backend_model(self) -> None:
        worker = self._load_worker_client()
        payload = worker.build_payload(
            source="doc-sync",
            event="sync_alert",
            title="同步告警",
            summary="作业 wecom.doc.2 连续失败",
            level="warn",
            text_segments=["上次成功：2026-08-29"],
            fields=[("作业", "wecom.doc.2"), ("失败次数", "3")],
            link=("查看任务", "https://hydwang.xyz/sync/"),
        )
        payload["dedup_key"] = worker.default_dedup_key(payload)

        notification = Notification.model_validate(payload)
        self.assertEqual(notification.source, "doc-sync")
        self.assertEqual(notification.level, "warn")
        self.assertEqual(notification.link.url, "https://hydwang.xyz/sync/")
        kinds = [segment.kind for segment in notification.segments]
        self.assertEqual(kinds, ["fields", "text"])
        self.assertIn("作业", notification.plain_text())

    def test_worker_payload_survives_the_storage_roundtrip(self) -> None:
        """worker 写的行会被 flush 读回来重投，所以 from_stored 也必须认它。"""
        worker = self._load_worker_client()
        payload = worker.build_payload(source="tplus", event="parent_match", title="T+ 核对")
        payload["dedup_key"] = worker.default_dedup_key(payload)
        restored = Notification.from_stored(payload)
        self.assertEqual(restored.title, "T+ 核对")

    def test_worker_dedup_key_is_stable_for_identical_content(self) -> None:
        worker = self._load_worker_client()
        first = worker.build_payload(source="doc-sync", event="e", title="t", summary="s")
        second = worker.build_payload(source="doc-sync", event="e", title="t", summary="s")
        # occurred_at 不参与 worker 侧的 key，否则同一条告警每轮都会重复投递
        self.assertEqual(worker.default_dedup_key(first), worker.default_dedup_key(second))


if __name__ == "__main__":
    unittest.main()
