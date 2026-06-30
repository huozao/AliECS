from __future__ import annotations

import sys
import unittest
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"
sys.path.insert(0, str(WORKER_ROOT))


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _msg_frame(msgid: str, chatid: str, content: str, *, userid: str = "WangHao") -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "x"},
        "body": {
            "msgid": msgid,
            "chatid": chatid,
            "chattype": "group",
            "from": {"userid": userid},
            "msgtype": "text",
            "text": {"content": content},
            "response_url": "http://resp",
        },
    }


class FakeStore:
    def __init__(self) -> None:
        self.bindings: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.assigned: list[tuple[str, str]] = []
        self.nodes: list[tuple[str, str, str]] = []

    def get_group_binding(self, chatid: str):
        return self.bindings.get(chatid)

    def upsert_group_binding(self, **kw) -> None:
        self.bindings[kw["chatid"]] = kw

    def assign_chat_messages_to_record(self, chatid: str, record_id: str) -> int:
        self.assigned.append((chatid, record_id))
        return 0

    def insert_group_message(self, **kw) -> bool:
        if kw["msgid"] in self.messages:
            return False
        self.messages[kw["msgid"]] = kw
        return True

    def mark_message_node(self, msgid: str, category: str = "", summary: str = "") -> bool:
        self.nodes.append((msgid, category, summary))
        return True


class FakeClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []

    def reply(self, response_url: str, text: str) -> dict:
        self.replies.append((response_url, text))
        return {}


class GroupListenerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _clear_app_modules()

    def tearDown(self) -> None:
        _clear_app_modules()

    def _mod(self):
        import app.pipelines.group_message_listener as mod

        return mod

    # ---- 纯函数 ----
    def test_parse_callback_text(self) -> None:
        m = self._mod().parse_callback(_msg_frame("m1", "c1", "hi @bot"))
        self.assertEqual("m1", m["msgid"])
        self.assertEqual("c1", m["chatid"])
        self.assertEqual("WangHao", m["from_userid"])
        self.assertEqual("hi @bot", m["text_content"])

    def test_parse_callback_ignores_non_message(self) -> None:
        self.assertIsNone(self._mod().parse_callback({"headers": {"req_id": "p"}, "errcode": 0}))
        self.assertIsNone(self._mod().parse_callback({"cmd": "aibot_msg_callback", "body": {}}))

    def test_find_binding_matches(self) -> None:
        mod = self._mod()
        index = {"202603200003": {"record_id": "r1"}, "202604070007": {"record_id": "r2"}}
        self.assertEqual(["202603200003"], mod.find_binding_matches("群名 覆膜蓝 202603200003 雷总", index))
        self.assertEqual([], mod.find_binding_matches("没有编号", index))
        self.assertEqual(2, len(mod.find_binding_matches("202603200003 与 202604070007", index)))

    def test_parse_node_command(self) -> None:
        mod = self._mod()
        self.assertEqual((True, "打样", "完成第一版"), mod.parse_node_command("@bot #节点 打样 完成第一版"))
        self.assertEqual((False, "", ""), mod.parse_node_command("普通消息"))
        is_node, cat, summary = mod.parse_node_command("#节点 这是一段没有类型的长摘要文本")
        self.assertTrue(is_node)
        self.assertEqual("", cat)

    def test_build_requirement_index(self) -> None:
        mod = self._mod()

        class FakeSheet:
            def get_sheets(self, docid):
                return [{"sheet_id": "s1", "title": "配色&样品需求单"}]

            def get_fields(self, docid, sheet_id):
                return {"fields": [
                    {"field_id": "f_no", "field_title": "审批单编号"},
                    {"field_id": "f_link", "field_title": "审批链接"},
                ]}

            def get_records(self, docid, sheet_id):
                return {"records": [{
                    "record_id": "r1",
                    "values": {
                        "f_no": [{"text": "202603200003", "type": "text"}],
                        "f_link": [{"link": "https://app.work.weixin.qq.com/x?sp_no=202604070007", "type": "url"}],
                    },
                }]}

        sheet_id, index = mod.build_requirement_index(FakeSheet(), "doc1", "配色&样品需求单")
        self.assertEqual("s1", sheet_id)
        self.assertEqual("r1", index["202603200003"]["record_id"])
        self.assertEqual("r1", index["202604070007"]["record_id"])

    # ---- handle_frame ----
    def _handle(self, frame, store, client, index):
        return self._mod().handle_frame(
            frame, store=store, client=client, index=index,
            profile="COMPANY_B", docid="doc1", sheet_title="配色&样品需求单",
        )

    def test_handle_unbound_with_code_binds(self) -> None:
        store, client = FakeStore(), FakeClient()
        index = {"202603200003": {"record_id": "r1", "requirement_key": "202603200003"}}
        action = self._handle(_msg_frame("m1", "c1", "覆膜蓝 202603200003 雷总"), store, client, index)
        self.assertEqual("bound", action)
        self.assertEqual("r1", store.bindings["c1"]["record_id"])
        self.assertTrue(client.replies and "已关联" in client.replies[0][1])

    def test_handle_bound_stores_message(self) -> None:
        store, client = FakeStore(), FakeClient()
        store.bindings["c1"] = {"record_id": "r9"}
        action = self._handle(_msg_frame("m2", "c1", "讨论：改配方"), store, client, {})
        self.assertEqual("stored", action)
        self.assertEqual("r9", store.messages["m2"]["record_id"])

    def test_handle_unbound_no_code_guides_and_stores(self) -> None:
        store, client = FakeStore(), FakeClient()
        action = self._handle(_msg_frame("m3", "c2", "你好"), store, client, {})
        self.assertEqual("stored", action)
        self.assertIn("m3", store.messages)
        self.assertTrue(any("尚未关联" in t for _, t in client.replies))

    def test_handle_dedupe(self) -> None:
        store, client = FakeStore(), FakeClient()
        store.bindings["c1"] = {"record_id": "r9"}
        self._handle(_msg_frame("m4", "c1", "a"), store, client, {})
        action = self._handle(_msg_frame("m4", "c1", "a"), store, client, {})
        self.assertEqual("dup", action)

    def test_handle_node_command(self) -> None:
        store, client = FakeStore(), FakeClient()
        store.bindings["c1"] = {"record_id": "r9"}
        action = self._handle(_msg_frame("m5", "c1", "#节点 打样 完成第一版"), store, client, {})
        self.assertEqual("node", action)
        self.assertEqual([("m5", "打样", "完成第一版")], store.nodes)


if __name__ == "__main__":
    unittest.main()
