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


class FakeSmartsheetClient:
    """备份簿 4 表齐全的假客户端，可按表注入字段列表。"""

    def __init__(self, fields_by_title: dict[str, list[str]]) -> None:
        self.fields_by_title = fields_by_title
        self.sheet_ids = {title: f"SID{i}" for i, title in enumerate(fields_by_title, start=1)}
        self.add_sheet_calls: list[tuple] = []
        self.add_fields_calls: list[tuple] = []

    def get_sheets(self, docid):
        return [
            {"sheet_id": sheet_id, "properties": {"title": title}}
            for title, sheet_id in self.sheet_ids.items()
        ]

    def get_fields(self, docid, sheet_id):
        title = next(t for t, s in self.sheet_ids.items() if s == sheet_id)
        return {
            "fields": [
                {"field_id": f"F{i}", "field_title": name, "field_type": "FIELD_TYPE_TEXT"}
                for i, name in enumerate(self.fields_by_title[title], start=1)
            ]
        }

    def add_sheet(self, docid, title, index=None):
        self.add_sheet_calls.append((docid, title, index))
        return {}

    def add_fields(self, docid, sheet_id, fields):
        self.add_fields_calls.append((docid, sheet_id, fields))
        return {}


class EnsureBackupWorkbookTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _clear_app_modules()
        from app.pipelines import wecom_structure_backup as module

        self.module = module
        self.max_sheets = 20

    def _full_titles(self) -> dict[str, list[str]]:
        return {
            title: self.module.backup_field_titles(title, max_sheets=self.max_sheets)
            for title in self.module.BACKUP_SHEET_TITLES
        }

    def test_extra_column_does_not_trigger_rebuild_or_writes(self) -> None:
        """既有表多出历史遗留列（如 docid）时不得重建、不得加列。"""
        fields = self._full_titles()
        fields["企微B-最新结构"] = fields["企微B-最新结构"][:5] + ["docid"] + fields["企微B-最新结构"][5:]
        client = FakeSmartsheetClient(fields)
        result = self.module.ensure_backup_workbook(
            client, docid="DOC1", admin_users=[], max_sheets=self.max_sheets
        )
        self.assertEqual([], client.add_sheet_calls)
        self.assertEqual([], client.add_fields_calls)
        self.assertEqual(client.sheet_ids["企微B-最新结构"], result["sheets"]["企微B-最新结构"])

    def test_missing_columns_added_without_rebuild(self) -> None:
        """既有表缺列时只补缺失列。"""
        fields = self._full_titles()
        removed = fields["飞书-最新结构"][-2:]
        fields["飞书-最新结构"] = fields["飞书-最新结构"][:-2]
        client = FakeSmartsheetClient(fields)
        self.module.ensure_backup_workbook(
            client, docid="DOC1", admin_users=[], max_sheets=self.max_sheets
        )
        self.assertEqual([], client.add_sheet_calls)
        added = [
            field["field_title"]
            for _, sheet_id, batch in client.add_fields_calls
            for field in batch
            if sheet_id == client.sheet_ids["飞书-最新结构"]
        ]
        self.assertEqual(removed, added)

    def test_snapshot_values_have_no_docid_key(self) -> None:
        """values 里不得出现名为 docid 的键（企微服务端会误当本企业 docid 校验）。"""
        source = {
            "provider": "wecom",
            "env_profile": "COMPANY_B",
            "external_doc_id": "dcFOREIGN",
            "document_name": "登记表",
            "source_url": "https://doc.weixin.qq.com/smartsheet/x",
            "source_type": "smartsheet_doc",
            "status": "active",
        }
        snapshot = self.module.build_document_snapshot(source, [], max_sheets=self.max_sheets)
        self.assertNotIn("docid", snapshot.values)
        self.assertEqual("dcFOREIGN", snapshot.values["文档定位ID"])


class AddSheetIndexTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _clear_app_modules()

    def test_add_sheet_passes_optional_index(self) -> None:
        from app.providers.wecom import WeComSmartsheetClient

        client = WeComSmartsheetClient("corp", "secret")
        calls = []
        client._post = lambda path, payload: calls.append((path, payload)) or {}
        client.add_sheet("DOC1", "表A")
        client.add_sheet("DOC1", "表B", 2)
        self.assertEqual({"docid": "DOC1", "properties": {"title": "表A"}}, calls[0][1])
        self.assertEqual({"docid": "DOC1", "properties": {"title": "表B", "index": 2}}, calls[1][1])


if __name__ == "__main__":
    unittest.main()
