from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def _write_stock_excel(directory: Path) -> Path:
    path = directory / "current_stock_20260610_120000.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(
        [
            "WarehouseCode", "WarehouseName", "InventoryCode", "InventoryName",
            "InventoryClassName", "Specification", "UnitName", "ExistingQuantity", "AvailableQuantity",
        ]
    )
    ws.append(["001", "原材库", "10001001", "ABS树脂0215H", "原材料", "", "kg", "232.0825", "232.0825"])
    ws.append(["001", "原材库", "20002", "3R蓝", "色粉", "", "kg", "5", "5"])
    ws.append(["002", "成品库", "30122027", "BX3027海灰色母", "物料清单", "PP", "kg", "100", "90"])
    wb.save(path)
    return path


class InventoryCurrentStockTests(unittest.TestCase):
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
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TPLUS_EXPORT_DIR"] = self._tmp.name

    def tearDown(self) -> None:
        if self._old_dir is None:
            os.environ.pop("TPLUS_EXPORT_DIR", None)
        else:
            os.environ["TPLUS_EXPORT_DIR"] = self._old_dir
        self._tmp.cleanup()

    def _user(self, *, roles=None, permissions=None) -> dict:
        return {"sub": "tester", "roles": roles or [], "permissions": permissions or []}

    def test_requires_inventory_permission(self) -> None:
        from fastapi import HTTPException

        _write_stock_excel(Path(self._tmp.name))
        with self.assertRaises(HTTPException) as ctx:
            self.main.inventory_current_stock(q="", warehouse="", user=self._user())
        self.assertEqual(403, ctx.exception.status_code)

    def test_missing_export_returns_404(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.main.inventory_current_stock(q="", warehouse="", user=self._user(roles=["admin"]))
        self.assertEqual(404, ctx.exception.status_code)

    def test_filters_by_warehouse_and_keyword(self) -> None:
        _write_stock_excel(Path(self._tmp.name))
        user = self._user(permissions=["inventory.raw.read"])

        all_rows = self.main.inventory_current_stock(q="", warehouse="", user=user)
        self.assertEqual(3, all_rows["total"])
        self.assertEqual(2, len(all_rows["warehouses"]))

        raw_only = self.main.inventory_current_stock(q="", warehouse="001", user=user)
        self.assertEqual(2, raw_only["total"])
        self.assertTrue(all(item["WarehouseCode"] == "001" for item in raw_only["items"]))

        keyword = self.main.inventory_current_stock(q="abs", warehouse="", user=user)
        self.assertEqual(1, keyword["total"])
        self.assertEqual("ABS树脂0215H", keyword["items"][0]["InventoryName"])
        self.assertAlmostEqual(232.0825, keyword["items"][0]["ExistingQuantity"])


if __name__ == "__main__":
    unittest.main()
