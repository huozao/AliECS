from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class FormulaColorsParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        self._old_secret = os.environ.get("AUTH_TOKEN_SECRET")
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]
        sys.path.insert(0, backend_root)
        os.environ["AUTH_TOKEN_SECRET"] = "test-formula-colors-secret"
        from app.routers import formula_colors

        self.module = formula_colors

    def tearDown(self) -> None:
        sys.path[:] = self._old_sys_path
        if self._old_secret is None:
            os.environ.pop("AUTH_TOKEN_SECRET", None)
        else:
            os.environ["AUTH_TOKEN_SECRET"] = self._old_secret
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]

    def test_interval_parses_sheet_notation(self) -> None:
        payload = {"ΔL*合格（内控）": "[-0.50, -0.20]"}
        self.assertEqual(self.module._interval(payload, "ΔL*合格（内控）"), [-0.5, -0.2])

    def test_interval_normalizes_descending_pair(self) -> None:
        # 表里历史上出现过写反的区间，闭区间必须左小右大，否则容差盒尺寸会变成负数。
        payload = {"Δb*合格": "[0.20, -0.10]"}
        self.assertEqual(self.module._interval(payload, "Δb*合格"), [-0.1, 0.2])

    def test_interval_rejects_free_text(self) -> None:
        for text in ("<0.5", "-0.5~-0.2", "", "[-0.5]"):
            self.assertIsNone(self.module._interval({"k": text}, "k"), text)

    def test_match_status_distinguishes_missing_code_from_blank(self) -> None:
        self.assertEqual(self.module._match_status("40000019", "0539-耐候ABS  玄武灰色母"), "matched")
        self.assertEqual(self.module._match_status("HYD-6800新", None), "code_missing")
        self.assertEqual(self.module._match_status("", None), "no_parent_code")

    def test_build_item_maps_sheet_columns(self) -> None:
        payload = {
            "型号": "0539-ABS耐候玄武灰",
            "父件编码": "40000019",
            "L*（客户标准）": "44.41",
            "a*": "-0.28",
            "b*": "1.43",
            "ΔL*合格（内控）": "[-0.30, 0.10]",
            "Δa*合格": "[-0.30, 0.00]",
            "Δb*合格": "[-0.30, 0.15]",
            "打样基料": "ABS121H",
            "添加比例": "8",
            "ΔE": "<0.5",
            "版本号": "20250208",
            "公司": "美的",
        }
        item = self.module._build_item("rec-1", payload, "0539-耐候ABS  玄武灰色母", "20250208")
        self.assertEqual(item["lab"], [44.41, -0.28, 1.43])
        self.assertEqual(item["tolerance"], [[-0.3, 0.1], [-0.3, 0.0], [-0.3, 0.15]])
        self.assertEqual(item["match_status"], "matched")
        self.assertEqual(item["parent_name"], "0539-耐候ABS  玄武灰色母")
        self.assertEqual(item["base_resin"], "ABS121H")
        self.assertEqual(item["dosage"], 8.0)

    def test_build_item_tolerates_missing_lab_and_tolerance(self) -> None:
        item = self.module._build_item("rec-2", {"型号": "只有名字"}, None, None)
        self.assertIsNone(item["lab"])
        self.assertEqual(item["tolerance"], [None, None, None])
        self.assertEqual(item["match_status"], "no_parent_code")

    def test_active_bom_join_filters_superseded_records(self) -> None:
        # tplus_bom_records 按版本累积，取父件名称必须只认 T+ 当前仍存在的那条。
        self.assertIn("missing_since IS NULL", self.module._RECORD_SQL)
        self.assertIn("DISTINCT ON (raw_json->>'Code')", self.module._RECORD_SQL)

    def test_record_query_also_matches_inventory_masters(self) -> None:
        self.assertIn("active_inventory", self.module._RECORD_SQL)
        self.assertIn("tplus_inventory_records", self.module._RECORD_SQL)
        self.assertIn("COALESCE(b.name, i.name)", self.module._RECORD_SQL)
        self.assertIn("InventoryClass", self.module._RECORD_SQL)
        self.assertIn("'06'", self.module._RECORD_SQL)
    def test_source_is_located_by_document_and_sheet_name(self) -> None:
        self.assertEqual(self.module.SOURCE_DOCUMENT, "标准型号0117")
        self.assertEqual(self.module.SOURCE_SHEET, "标准型号规格&月统计")
        self.assertNotIn("20120", self.module._SOURCE_SQL)

    def test_refresh_enqueues_sheet_sync_request(self) -> None:
        self.assertIn("INSERT INTO sync_requests", self.module._ENQUEUE_SQL)
        self.assertIn("'manual', 'pending'", self.module._ENQUEUE_SQL)
        self.assertIn("status IN ('pending', 'running')", self.module._ENQUEUE_DEDUP_SQL)

    def test_refresh_dedup_is_time_bounded(self) -> None:
        """去重不设时限时，一条卡在 running 的请求会让「刷新数据」永久返回
        already_pending——按钮再也入不了队。"""
        self.assertIn("requested_at", self.module._ENQUEUE_DEDUP_SQL)
        self.assertIn("INTERVAL", self.module._ENQUEUE_DEDUP_SQL.upper())

    def test_refresh_router_exists_and_reuses_page_permission(self) -> None:
        import inspect
        self.assertTrue(hasattr(self.module, "formula_colors_refresh"))
        source = inspect.getsource(self.module.formula_colors_refresh)
        self.assertIn("require_permission(", source)

if __name__ == "__main__":
    unittest.main()
