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


class FakeContactStore:
    def __init__(self) -> None:
        self.contacts: dict[tuple[str, str], dict] = {}

    def upsert_managed_contact(self, contact: dict) -> None:
        self.contacts[(contact["channel"], contact["peer_id"])] = dict(contact)


class ManagedContactsSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        _clear_app_modules()
        worker_root = str(WORKER_ROOT)
        sys.path[:] = [item for item in sys.path if item != worker_root]
        sys.path.insert(0, worker_root)

    def tearDown(self) -> None:
        _clear_app_modules()
        sys.path[:] = self._old_sys_path

    def test_wechat_sheet_upserts_contact_and_applies_sheet_changes(self) -> None:
        from app.pipelines.managed_contacts import sync_managed_contacts_from_sheet

        store = FakeContactStore()

        sync_managed_contacts_from_sheet(
            store,
            "微信用户清单",
            [
                {
                    "peer_id": "wxid_a",
                    "display_name": "张三",
                    "enabled": "是",
                    "project_url": "https://chatgpt.com/g/p1/project",
                }
            ],
        )
        row = store.contacts[("wechat", "wxid_a")]
        self.assertTrue(row["enabled"])
        self.assertEqual("张三", row["display_name"])
        self.assertEqual("https://chatgpt.com/g/p1/project", row["project_url"])

        sync_managed_contacts_from_sheet(
            store,
            "微信用户清单",
            [{"peer_id": "wxid_a", "enabled": "否", "project_url": "https://chatgpt.com/g/p2/project"}],
        )
        row = store.contacts[("wechat", "wxid_a")]
        self.assertFalse(row["enabled"])
        self.assertTrue(row["project_url"].endswith("/p2/project"))

    def test_feishu_sheet_maps_to_same_table_with_feishu_channel(self) -> None:
        from app.pipelines.managed_contacts import sync_managed_contacts_from_sheet

        store = FakeContactStore()
        sync_managed_contacts_from_sheet(
            store,
            "飞书用户清单",
            [{"peer_id": "ou_x", "display_name": "李四", "enabled": "true"}],
        )

        self.assertEqual("李四", store.contacts[("feishu", "user:ou_x")]["display_name"])

    def test_feishu_session_index_upserts_current_active_session_route(self) -> None:
        from app.pipelines.managed_contacts import sync_managed_contacts_from_sheet

        store = FakeContactStore()
        changed = sync_managed_contacts_from_sheet(
            store,
            "会话索引表",
            [
                {
                    "session_key": "tenant-a:user:ou_28d4",
                    "会话类型": "私聊",
                    "飞书用户名": "hao",
                    "ChatGPT 项目名": "飞书 AI 会话台",
                    "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark/project",
                    "会话状态": "活跃",
                    "是否当前会话": True,
                }
            ],
        )

        self.assertEqual(1, changed)
        row = store.contacts[("feishu", "user:ou_28d4")]
        self.assertEqual("hao", row["display_name"])
        self.assertEqual("https://chatgpt.com/g/g-p-lark/project", row["project_url"])
        self.assertEqual("飞书 AI 会话台", row["project_name"])

    def test_feishu_session_index_keeps_private_and_group_routes_separate(self) -> None:
        from app.pipelines.managed_contacts import sync_managed_contacts_from_sheet

        store = FakeContactStore()
        changed = sync_managed_contacts_from_sheet(
            store,
            "会话索引表",
            [
                {
                    "session_key": "tenant-a:user:ou_28d4",
                    "会话类型": "私聊",
                    "飞书用户名": "hao",
                    "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark-dm/project",
                    "会话状态": "活跃",
                    "是否当前会话": True,
                },
                {
                    "session_key": "tenant-a:group:oc_group1",
                    "会话类型": "群聊",
                    "飞书群名": "项目群",
                    "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark-group/project",
                    "会话状态": "活跃",
                    "是否当前会话": True,
                },
                {
                    "session_key": "tenant-a:group_user:oc_group1:ou_28d4",
                    "会话类型": "群内个人",
                    "会话名称": "项目群 / hao",
                    "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark-group-user/project",
                    "会话状态": "活跃",
                    "是否当前会话": True,
                },
            ],
        )

        self.assertEqual(3, changed)
        self.assertIn(("feishu", "user:ou_28d4"), store.contacts)
        self.assertIn(("feishu", "group:oc_group1"), store.contacts)
        self.assertIn(("feishu", "group_user:oc_group1:ou_28d4"), store.contacts)
        self.assertNotIn(("feishu", "ou_28d4"), store.contacts)
        self.assertNotIn(("feishu", "oc_group1"), store.contacts)

    def test_feishu_session_index_ignores_non_current_or_missing_status_rows(self) -> None:
        from app.pipelines.managed_contacts import sync_managed_contacts_from_sheet

        store = FakeContactStore()
        changed = sync_managed_contacts_from_sheet(
            store,
            "会话索引表",
            [
                {
                    "session_key": "tenant-a:user:ou_old",
                    "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark/project",
                    "会话状态": "活跃",
                    "是否当前会话": False,
                },
                {
                    "session_key": "tenant-a:user:ou_missing_status",
                    "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark/project",
                    "是否当前会话": True,
                },
                {
                    "session_key": "tenant-a:user:ou_archived",
                    "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark/project",
                    "会话状态": "已归档",
                    "是否当前会话": True,
                },
            ],
        )

        self.assertEqual(0, changed)
        self.assertEqual({}, store.contacts)

    def test_control_panel_sheet_aliases_match_runtime_columns(self) -> None:
        from app.pipelines.managed_contacts import sync_managed_contacts_from_sheet

        store = FakeContactStore()
        sync_managed_contacts_from_sheet(
            store,
            "微信用户清单",
            [
                {
                    "peer_id": "o9cq80whD47YZs0xR1Y9Ih8rdVnc_im.wechat",
                    "微信名称": "微信A",
                    "新对话链接": "https://chatgpt.com/g/g-p-weixin-a/project",
                    "所属项目名称": "weixin-a",
                }
            ],
        )

        row = store.contacts[("wechat", "o9cq80whD47YZs0xR1Y9Ih8rdVnc_im.wechat")]
        self.assertEqual("微信A", row["display_name"])
        self.assertEqual("https://chatgpt.com/g/g-p-weixin-a/project", row["project_url"])
        self.assertEqual("weixin-a", row["project_name"])

    def test_unknown_sheet_is_ignored(self) -> None:
        from app.pipelines.managed_contacts import sync_managed_contacts_from_sheet

        store = FakeContactStore()
        changed = sync_managed_contacts_from_sheet(store, "普通业务表", [{"peer_id": "wxid_a"}])

        self.assertEqual(0, changed)
        self.assertEqual({}, store.contacts)


if __name__ == "__main__":
    unittest.main()
