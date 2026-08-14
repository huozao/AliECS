from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class BackendExportsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        from app.routers import exports as main_module

        cls.main = main_module

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]

    def setUp(self) -> None:
        self._old_dir = os.environ.get("TPLUS_EXPORT_DIR")

    def tearDown(self) -> None:
        if self._old_dir is None:
            os.environ.pop("TPLUS_EXPORT_DIR", None)
        else:
            os.environ["TPLUS_EXPORT_DIR"] = self._old_dir

    def test_tplus_module_of_strips_timestamp_suffix(self) -> None:
        self.assertEqual("inventory", self.main._tplus_module_of("inventory_20260607_145038.xlsx"))
        self.assertEqual("purchase_arrival_list", self.main._tplus_module_of("purchase_arrival_list_20260607_145038.xlsx"))
        self.assertEqual("oddname", self.main._tplus_module_of("oddname.xlsx"))

    def test_latest_tplus_exports_picks_newest_per_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in (
                "inventory_20260601_000000.xlsx",
                "inventory_20260607_145038.xlsx",
                "bom_20260608_065323.xlsx",
            ):
                (Path(tmp) / name).write_bytes(b"x")
            os.environ["TPLUS_EXPORT_DIR"] = tmp

            items = self.main._latest_tplus_exports()

        names = {item["name"]: item["file_name"] for item in items}
        self.assertEqual({"bom": "bom_20260608_065323.xlsx", "inventory": "inventory_20260607_145038.xlsx"}, names)
        self.assertTrue(all(item["download_url"].startswith("/v1/exports/tplus/") for item in items))

    def test_latest_tplus_exports_includes_short_descriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in (
                "inventory_20260607_145038.xlsx",
                "purchase_order_list_20260607_145038.xlsx",
                "purchase_price_20260607_145038.xlsx",
                "sales_price_20260607_145038.xlsx",
                "unknown_module_20260607_145038.xlsx",
            ):
                (Path(tmp) / name).write_bytes(b"x")
            os.environ["TPLUS_EXPORT_DIR"] = tmp

            items = {item["name"]: item for item in self.main._latest_tplus_exports()}

        self.assertIn("销售价格", items["inventory"]["description"])
        self.assertIn("不含明细单价金额", items["purchase_order_list"]["description"])
        self.assertIn("采购价格表", items["purchase_price"]["description"])
        self.assertIn("销售价格表", items["sales_price"]["description"])
        self.assertIn("暂未配置说明", items["unknown_module"]["description"])

    def test_tplus_export_description_uses_system_config_mirror(self) -> None:
        old_record = self.main._system_config_record
        self.main._system_config_record = lambda sheet: {"bom": "配置里的 BOM 说明"} if sheet == "T+导出说明" else {}
        try:
            self.assertEqual("配置里的 BOM 说明", self.main._tplus_export_description("bom"))
        finally:
            self.main._system_config_record = old_record

    def test_tplus_export_description_falls_back_when_mirror_empty(self) -> None:
        old_record = self.main._system_config_record
        self.main._system_config_record = lambda sheet: {}
        try:
            self.assertIn("BOM 父件和子件", self.main._tplus_export_description("bom"))
            self.assertIn("暂未配置说明", self.main._tplus_export_description("unknown_module"))
        finally:
            self.main._system_config_record = old_record

    def test_config_text_reads_feishu_rich_text_cell(self) -> None:
        self.assertEqual("配置里的 BOM 说明", self.main._config_text([{"text": "配置里的 BOM 说明"}]))

    def test_system_config_record_maps_raw_field_ids_to_titles(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.rows = []

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def execute(self, sql: str, params=None) -> None:
                if "FROM external_sources es" in sql:
                    self.rows = [
                        (
                            {"fields": {"fld_id": "global-default", "fld_bom": [{"text": "映射说明"}]}},
                            {"配置编号": "global-default", "bom": "归一化说明"},
                            101,
                        )
                    ]
                else:
                    self.rows = [(101, "fld_id", "配置编号"), (101, "fld_bom", "bom")]

            def fetchall(self):
                return self.rows

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self) -> None:
                return None

        old_conn = self.main._conn
        self.main._conn = lambda: FakeConn()
        try:
            record = self.main._system_config_record("T+导出说明")
        finally:
            self.main._conn = old_conn
        self.assertEqual([{"text": "映射说明"}], record["bom"])

    def test_exports_catalog_includes_doc_rows_before_sheet_sync(self) -> None:
        executed_sql: list[str] = []

        class FakeCursor:
            def __init__(self) -> None:
                self.rows = []

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def execute(self, sql: str, params=None) -> None:
                executed_sql.append(sql)
                self.rows = [(
                    "wecom", "COMPANY_A", "dc" + "x" * 86, "smartsheet_doc", "新副本", "新副本",
                    42, 0, 0, None, "verified", {"read": "verified", "copy": "allowed"}, "active",
                )]

            def fetchall(self):
                return self.rows

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self) -> None:
                return None

        old_conn = self.main._conn
        old_latest = self.main._latest_tplus_exports
        self.main._conn = lambda: FakeConn()
        self.main._latest_tplus_exports = lambda: []
        try:
            catalog = self.main.exports_catalog(_={})
        finally:
            self.main._conn = old_conn
            self.main._latest_tplus_exports = old_latest

        wecom_a = next(tab for tab in catalog["tabs"] if tab["key"] == "wecom_company_a")
        self.assertIn("document_locator_registry", executed_sql[0])
        self.assertEqual(1, len(wecom_a["items"]))
        self.assertEqual(42, wecom_a["items"][0]["source_id"])
        self.assertEqual("新副本", wecom_a["items"][0]["name"])
        self.assertTrue(wecom_a["items"][0]["can_sync"])
        self.assertFalse(wecom_a["items"][0]["can_download"])

    def test_legacy_sync_routes_delegate_to_shared_control_service(self) -> None:
        with patch.object(self.main, "_conn"), patch.object(
            self.main.sync_control,
            "enqueue_doc_asset",
            return_value={"queued": True, "request_id": 1, "status": "pending", "document_name": "生产表"},
        ) as enqueue_doc, patch.object(
            self.main.sync_control,
            "enqueue_all",
            return_value={"documents_queued": 2, "documents_skipped": 0, "tplus_queued": True, "message": "queued"},
        ) as enqueue_all:
            single = self.main.exports_external_doc_sync(17, user={"sub": "admin"})
            all_result = self.main.exports_sync_all(user={"sub": "admin"})

        self.assertEqual(1, single["requests_created"])
        self.assertEqual(2, all_result["requests_created"])
        enqueue_doc.assert_called_once_with(unittest.mock.ANY, 17, "admin")
        enqueue_all.assert_called_once_with(unittest.mock.ANY, "admin")

    def test_structure_backup_download_uses_authoritative_locator_tables(self) -> None:
        from openpyxl import Workbook

        class FakeCursor:
            def __init__(self) -> None:
                self.query_index = 0

            def execute(self, sql: str, params=None) -> None:
                del params
                self.query_index += 1
                self.last_sql = sql

            def fetchall(self):
                if self.query_index == 1:
                    return [(
                        41, "wecom", "COMPANY_A", "生产表", "dc-synthetic", "https://example.invalid/share",
                        ["admin-one"], "COMPANY_A#1", "registry", "active", "verified", "",
                        {"read": "verified", "write": "unknown", "copy": "allowed"}, 3,
                        "2026-08-13", "2026-08-14", "2026-08-14", "2026-08-14",
                    )]
                return [(
                    "2026-08-14", "生产表", "sync-success", "worker", ["last_sync_at"],
                    {"syncability_status": "verified"}, 41,
                )]

        workbook = Workbook()
        workbook.remove(workbook.active)
        cursor = FakeCursor()

        self.main._append_locator_archive_worksheets(workbook, cursor)

        self.assertEqual(["文档定位档案", "定位档案变更历史"], workbook.sheetnames)
        self.assertEqual(list(self.main._LOCATOR_CURRENT_FIELDS), [cell.value for cell in workbook["文档定位档案"][1]])
        self.assertEqual(list(self.main._LOCATOR_EVENT_FIELDS), [cell.value for cell in workbook["定位档案变更历史"][1]])
        self.assertEqual("dc-synthetic", workbook["文档定位档案"]["D2"].value)
        self.assertEqual("locator:41", workbook["文档定位档案"]["T2"].value)
        self.assertEqual("locator:41", workbook["定位档案变更历史"]["G2"].value)

    def test_match_export_files_buckets_to_first_run_at_or_after_file_time(self):
        from app.routers.exports import _match_export_files_to_runs
        runs = [(252, "2026-06-24T10:10:00"), (251, "2026-06-24T10:08:00"), (250, "2026-06-24T09:00:00")]
        files = ["bom_20260624_100751.xlsx", "current_stock_20260624_100752.xlsx", "bom_20260624_085500.xlsx"]
        mapping = _match_export_files_to_runs(runs, files)
        self.assertEqual(["bom_20260624_100751.xlsx", "current_stock_20260624_100752.xlsx"],
                         sorted(mapping[251]))
        self.assertEqual(["bom_20260624_085500.xlsx"], mapping[250])
        self.assertNotIn(252, mapping)

    def test_match_export_files_handles_timezone_aware_run_datetimes(self):
        # psycopg returns timestamptz columns as tz-aware datetimes; file timestamps
        # parse to naive datetimes. Comparing the two must not raise TypeError.
        from datetime import datetime, timezone
        from app.routers.exports import _match_export_files_to_runs
        aware = datetime(2026, 6, 23, 22, 42, 2, 746980, tzinfo=timezone.utc)
        mapping = _match_export_files_to_runs([(253, aware)], ["bom_20260623_224150.xlsx"])
        self.assertEqual(["bom_20260623_224150.xlsx"], mapping[253])

    def test_tplus_download_rejects_traversal_and_non_xlsx(self) -> None:
        from fastapi import HTTPException

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TPLUS_EXPORT_DIR"] = tmp
            for bad in ("../../etc/passwd", "a/b.xlsx", "notes.txt"):
                with self.assertRaises(HTTPException) as ctx:
                    self.main.exports_tplus_download(bad, _={})
                self.assertEqual(400, ctx.exception.status_code)

            with self.assertRaises(HTTPException) as ctx:
                self.main.exports_tplus_download("absent_20260101_000000.xlsx", _={})
            self.assertEqual(404, ctx.exception.status_code)


if __name__ == "__main__":
    unittest.main()
