from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class BackendExportsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        from app import main as main_module

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

    def test_exports_catalog_includes_doc_rows_before_sheet_sync(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.rows = []

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def execute(self, sql: str, params=None) -> None:
                if "WHERE s.external_sheet_id <> '' AND s.status" in sql:
                    self.rows = []
                else:
                    self.rows = [("wecom", "COMPANY_A", "doc-new", "新副本", 42, 0, 0, None)]

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
        self.assertEqual(
            [
                {
                    "name": "新副本",
                    "source_id": 42,
                    "sheets": 0,
                    "rows": 0,
                    "updated_at": None,
                    "download_url": None,
                }
            ],
            wecom_a["items"],
        )

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
