from __future__ import annotations

import sys
import unittest
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _cells(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


class TplusParentMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        _clear_app_modules()
        worker_root = str(WORKER_ROOT)
        sys.path[:] = [item for item in sys.path if item != worker_root]
        sys.path.insert(0, worker_root)
        from app.pipelines import tplus_parent_match

        self.module = tplus_parent_match

    def tearDown(self) -> None:
        _clear_app_modules()
        sys.path[:] = self._old_sys_path

    def _plan(self, records, bom):
        return self.module.plan_updates(records, bom, "2026-08-04 03:00")

    def test_matched_row_fills_parent_name_from_tplus(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("40000019"), "型号": _cells("0539-ABS耐候玄武灰")}}]
        result = self._plan(records, {"40000019": ("0539-耐候ABS  玄武灰色母", "20250208")})
        self.assertEqual(result.ok, 1)
        self.assertEqual(result.updates[0]["values"]["父件名称"], _cells("0539-耐候ABS  玄武灰色母"))
        self.assertEqual(result.updates[0]["values"]["T+匹配状态"], _cells("一致"))

    def test_renamed_row_is_updated_and_reported(self) -> None:
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("06000002"), "型号": _cells("乌金灰"), "父件名称": _cells("9001-cscscs")}}]
        result = self._plan(records, {"06000002": ("HYD-9721乌金灰 改性", "260720")})
        self.assertEqual(len(result.renamed), 1)
        self.assertEqual(result.renamed[0][2:], ("9001-cscscs", "HYD-9721乌金灰 改性"))
        self.assertEqual(result.updates[0]["values"]["T+匹配状态"], _cells("名称已更新"))

    def test_missing_code_never_rewrites_the_code_or_name(self) -> None:
        """编码是执行主键，失联时只标状态——自动改主键判错会顺着执行链扩散。"""
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("HYD-6800新"), "型号": _cells("HYD-6800墨绿"), "父件名称": _cells("HYD-6800墨绿色母")}}]
        result = self._plan(records, {"HYD-6800X": ("HYD-6800墨绿色母", "20250816")})
        self.assertEqual(result.missing, [("HYD-6800新", "HYD-6800墨绿")])
        values = result.updates[0]["values"]
        self.assertNotIn("父件编码", values)
        self.assertNotIn("父件名称", values)
        self.assertEqual(values["T+匹配状态"], _cells("编码失联"))

    def test_blank_code_is_distinguished_from_missing_code(self) -> None:
        records = [{"record_id": "r1", "values": {"型号": _cells("只有型号")}}]
        result = self._plan(records, {})
        self.assertEqual(result.no_code, 1)
        self.assertEqual(result.missing, [])
        self.assertEqual(result.updates[0]["values"]["T+匹配状态"], _cells("无父件编码"))

    def test_unchanged_row_only_refreshes_checked_at(self) -> None:
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("40000019"), "父件名称": _cells("已经对了"), "T+匹配状态": _cells("一致")}}]
        result = self._plan(records, {"40000019": ("已经对了", "v1")})
        self.assertEqual(list(result.updates[0]["values"]), ["T+核对时间"])

    def test_alert_lists_missing_rows_and_says_code_untouched(self) -> None:
        records = [
            {"record_id": "r1", "values": {"父件编码": _cells("A"), "型号": _cells("甲")}},
            {"record_id": "r2", "values": {"父件编码": _cells("B"), "型号": _cells("乙"), "父件名称": _cells("旧名")}},
        ]
        result = self._plan(records, {"B": ("新名", "v1")})
        text = self.module.build_alert(result)
        self.assertIn("编码失联 1 行", text)
        self.assertIn("未自动改编码", text)
        self.assertIn("A｜甲", text)
        self.assertIn("旧名 → 新名", text)

    def test_alert_says_no_problem_when_everything_matches(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("甲")}}]
        text = self.module.build_alert(self._plan(records, {"A": ("甲", "v1")}))
        self.assertIn("✅ 无异常。", text)

    def test_active_bom_query_excludes_superseded_versions(self) -> None:
        self.assertIn("missing_since IS NULL", self.module._ACTIVE_BOM_SQL)
        self.assertIn("DISTINCT ON (raw_json->>'Code')", self.module._ACTIVE_BOM_SQL)

    def test_managed_fields_never_include_the_parent_code(self) -> None:
        self.assertNotIn(self.module.F_PARENT_CODE, self.module.MANAGED_FIELDS)
        self.assertEqual(self.module.MANAGED_FIELDS, ("父件名称", "T+匹配状态", "T+核对时间"))


if __name__ == "__main__":
    unittest.main()
