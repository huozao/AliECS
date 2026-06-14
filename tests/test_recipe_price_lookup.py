from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import Workbook

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class _BackendTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = [item for item in sys.path if item != str(BACKEND_ROOT)]
        sys.path.insert(0, str(BACKEND_ROOT))
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = self._old_sys_path


class PriceLookupTests(_BackendTestBase):
    def _write(self, rows: list[dict], name: str) -> Path:
        path = self.dir / name
        pd.DataFrame(rows).to_excel(path, index=False)
        return path

    def test_purchase_price_picks_newest_voucher_date(self) -> None:
        from app.recipes.price_lookup import latest_purchase_prices

        self._write(
            [
                {"存货编码": "RM-001", "单据日期": "2026-05-01", "含税单价": 10.0},
                {"存货编码": "RM-001", "单据日期": "2026-05-31", "含税单价": 12.5},
                {"存货编码": "RM-002", "单据日期": "2026-04-10", "含税单价": 7.0},
            ],
            "purchase_price_20260614_120000.xlsx",
        )
        prices = latest_purchase_prices(export_dir=self.dir)
        self.assertEqual(12.5, prices["RM-001"]["price"])
        self.assertEqual("2026-05-31", prices["RM-001"]["date"])
        self.assertEqual(7.0, prices["RM-002"]["price"])

    def test_purchase_price_ignores_zero_or_blank_price_rows(self) -> None:
        from app.recipes.price_lookup import latest_purchase_prices

        self._write(
            [
                {"存货编码": "RM-001", "单据日期": "2026-05-01", "含税单价": 12.5},
                {"存货编码": "RM-001", "单据日期": "2026-05-31", "含税单价": 0},
            ],
            "purchase_price_20260614_120000.xlsx",
        )
        prices = latest_purchase_prices(export_dir=self.dir)
        self.assertEqual(12.5, prices["RM-001"]["price"])
        self.assertEqual("2026-05-01", prices["RM-001"]["date"])

    def test_uses_newest_export_file(self) -> None:
        from app.recipes.price_lookup import latest_purchase_prices

        old = self._write(
            [{"存货编码": "RM-001", "单据日期": "2026-01-01", "含税单价": 5.0}],
            "purchase_price_20260101_000000.xlsx",
        )
        new = self._write(
            [{"存货编码": "RM-001", "单据日期": "2026-05-31", "含税单价": 12.5}],
            "purchase_price_20260614_120000.xlsx",
        )
        os.utime(old, (1_700_000_000, 1_700_000_000))
        os.utime(new, (1_800_000_000, 1_800_000_000))
        prices = latest_purchase_prices(export_dir=self.dir)
        self.assertEqual(12.5, prices["RM-001"]["price"])

    def test_sales_price_lookup(self) -> None:
        from app.recipes.price_lookup import latest_sales_prices

        self._write(
            [
                {"存货编码": "P-100", "单据日期": "2026-05-20", "含税单价": 28.0},
                {"存货编码": "P-100", "单据日期": "2026-06-01", "含税单价": 30.0},
            ],
            "sales_price_20260614_120000.xlsx",
        )
        prices = latest_sales_prices(export_dir=self.dir)
        self.assertEqual(30.0, prices["P-100"]["price"])
        self.assertEqual("2026-06-01", prices["P-100"]["date"])

    def test_missing_dir_or_file_returns_empty(self) -> None:
        from app.recipes.price_lookup import latest_purchase_prices, latest_sales_prices

        self.assertEqual({}, latest_purchase_prices(export_dir=self.dir / "nope"))
        self.assertEqual({}, latest_sales_prices(export_dir=self.dir))  # dir exists, no file


class RecipeCostPriceInjectionTests(_BackendTestBase):
    def _make_result(self):
        from app.recipes.bom_query import query_recipe_workbook

        src = self.dir / "bom_20260614.xlsx"
        wb = Workbook()
        material = wb.active
        material.title = "物料清单"
        material.append(["父件编码", "父件名称", "规格型号", "版本号", "计量单位", "生产数量", "默认BOM", "停用"])
        material.append(["P-100", "成品A", "X", "V1", "kg", 25, 1, 0])
        component = wb.create_sheet("子件明细")
        component.append(["版本号", "父件编码", "子件编码", "子件名称", "规格型号", "计量单位", "需用数量", "系统单价"])
        component.append(["V1", "P-100", "RM-001", "树脂", "A", "kg", 20, 3])
        component.append(["V1", "P-100", "RM-002", "色粉", "B", "kg", 5, 9])
        wb.save(src)
        return query_recipe_workbook(src, query_text="P-100", default_bom="1")

    def test_purchase_price_overrides_system_price_with_date(self) -> None:
        from app.recipes.bom_query import calculate_recipe_costs

        recipes = calculate_recipe_costs(
            self._make_result(),
            purchase_prices={"RM-001": {"price": 12.5, "date": "2026-05-31"}},
        )
        lines = {line["child_code"]: line for line in recipes[0]["lines"]}
        self.assertEqual(12.5, lines["RM-001"]["system_price"])
        self.assertEqual("2026-05-31", lines["RM-001"]["system_price_date"])
        # 无采购记录 -> 回退 BOM 系统单价，无日期
        self.assertEqual(9, lines["RM-002"]["system_price"])
        self.assertIsNone(lines["RM-002"]["system_price_date"])

    def test_sales_price_attached_to_recipe(self) -> None:
        from app.recipes.bom_query import calculate_recipe_costs

        result = self._make_result()
        recipes = calculate_recipe_costs(result, sales_prices={"P-100": {"price": 30.0, "date": "2026-06-01"}})
        self.assertEqual(30.0, recipes[0]["sales_price"])
        self.assertEqual("2026-06-01", recipes[0]["sales_price_date"])

        plain = calculate_recipe_costs(result)
        self.assertIsNone(plain[0]["sales_price"])
        self.assertIsNone(plain[0]["sales_price_date"])


if __name__ == "__main__":
    unittest.main()
