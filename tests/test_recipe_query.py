from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl import load_workbook


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))


class RecipeQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]
        sys.path.insert(0, str(BACKEND_ROOT))
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = self._old_sys_path

    def _write_source_workbook(self) -> Path:
        source = self.tmp_path / "bom_20260604.xlsx"
        material_rows = [
            {
                "父件编码": "30122027-3027",
                "父件名称": "BX3027-海尔洗衣机PP海灰色母",
                "规格型号": "PP",
                "版本号": "V1",
                "计量单位": "kg",
                "生产数量": 25,
                "默认BOM": 1,
                "停用": 0,
            },
            {
                "父件编码": "30122027-3027",
                "父件名称": "BX3027-停用旧版",
                "规格型号": "PP",
                "版本号": "V0",
                "计量单位": "kg",
                "生产数量": 25,
                "默认BOM": 0,
                "停用": 1,
            },
        ]
        child_rows = [
            {
                "版本号": "V1",
                "父件编码": "30122027-3027",
                "子件编码": "C001",
                "子件名称": "树脂",
                "规格型号": "A",
                "计量单位": "kg",
                "需用数量": 2,
            },
            {
                "版本号": "V1",
                "父件编码": "30122027-3027",
                "子件编码": "C002",
                "子件名称": "色粉",
                "规格型号": "B",
                "计量单位": "g",
                "需用数量": 500,
            },
            {
                "版本号": "V0",
                "父件编码": "30122027-3027",
                "子件编码": "C003",
                "子件名称": "旧版树脂",
                "规格型号": "C",
                "计量单位": "kg",
                "需用数量": 1,
            },
        ]
        with pd.ExcelWriter(source, engine="openpyxl") as writer:
            pd.DataFrame(material_rows).to_excel(writer, sheet_name="物料清单", index=False)
            pd.DataFrame(child_rows).to_excel(writer, sheet_name="子件明细", index=False)
        return source

    def test_query_merges_tplus_bom_workbook_and_keeps_disabled_versions(self) -> None:
        from app.recipes.bom_query import query_recipe_workbook

        result = query_recipe_workbook(self._write_source_workbook(), query_text="3027", default_bom="all")

        self.assertEqual(3, result.match_count)
        self.assertEqual(2, result.recipe_count)
        self.assertEqual({"V0", "V1"}, set(result.detail["版本号_子件"].astype(str)))
        disabled = result.detail[result.detail["版本号_子件"] == "V0"].iloc[0]
        self.assertEqual("1", str(disabled["停用"]))

    def test_query_can_filter_to_default_bom_only(self) -> None:
        from app.recipes.bom_query import query_recipe_workbook

        result = query_recipe_workbook(self._write_source_workbook(), query_text="3027", default_bom="1")

        self.assertEqual(2, result.match_count)
        self.assertEqual(1, result.recipe_count)
        self.assertEqual({"V1"}, set(result.detail["版本号_子件"].astype(str)))

    def test_query_preserves_leading_zero_codes_when_excel_infers_numeric_text(self) -> None:
        from app.recipes.bom_query import query_recipe_workbook

        source = self.tmp_path / "bom_leading_zero.xlsx"
        wb = Workbook()
        material = wb.active
        material.title = "物料清单"
        material.append(["父件编码", "父件名称", "规格型号", "版本号", "计量单位", "生产数量", "默认BOM", "停用"])
        material.append(["0003027", "BX3027-前导零配方", "PP", "V001", "kg", 25, 1, 0])
        component = wb.create_sheet("子件明细")
        component.append(["版本号", "父件编码", "子件编码", "子件名称", "规格型号", "计量单位", "需用数量"])
        component.append(["V001", "0003027", "03000012", "跳过项", "A", "kg", 2])
        component.append(["V001", "0003027", "0000008", "有效项", "B", "kg", 8])
        wb.save(source)

        result = query_recipe_workbook(source, query_text="3027", default_bom="1")

        self.assertEqual(2, result.match_count)
        self.assertEqual({"0003027"}, set(result.detail["父件编码"]))
        self.assertEqual({"03000012", "0000008"}, set(result.detail["子件编码"]))
        skipped = result.detail[result.detail["子件编码"] == "03000012"].iloc[0]
        included = result.detail[result.detail["子件编码"] == "0000008"].iloc[0]
        self.assertTrue(pd.isna(skipped["比例"]))
        self.assertEqual(1.0, included["比例"])

    def test_save_recipe_workbook_creates_human_review_and_matrix_sheets(self) -> None:
        from app.recipes.bom_query import query_recipe_workbook, save_recipe_workbook

        result = query_recipe_workbook(self._write_source_workbook(), query_text="3027", default_bom="1")
        output_path = self.tmp_path / "recipe.xlsx"

        save_recipe_workbook(output_path, result)

        wb = load_workbook(output_path, data_only=False)
        self.assertIn("配方表_人眼版", wb.sheetnames)
        self.assertIn("横向对比_矩阵", wb.sheetnames)
        self.assertIn("父件子件明细_提取", wb.sheetnames)
        self.assertIn("版本父件分组合计", wb.sheetnames)
        self.assertEqual("物料清单配方表", wb["配方表_人眼版"]["A1"].value)


if __name__ == "__main__":
    unittest.main()
