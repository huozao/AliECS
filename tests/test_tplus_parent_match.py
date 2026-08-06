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

    def _creates(self, records, bom):
        return self.module.plan_creates(records, bom, "2026-08-06 03:00")

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

    def test_unchanged_row_produces_no_write_at_all(self) -> None:
        """全表每天重写核对时间，在补建后会变成上千行的无效写入。"""
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("40000019"), "父件名称": _cells("已经对了"), "T+匹配状态": _cells("一致")}}]
        result = self._plan(records, {"40000019": ("已经对了", "v1")})
        self.assertEqual(result.updates, [])
        self.assertEqual(result.ok, 1)

    def test_changed_row_still_carries_the_checked_at_stamp(self) -> None:
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("40000019"), "父件名称": _cells("旧名"), "T+匹配状态": _cells("一致")}}]
        result = self._plan(records, {"40000019": ("新名", "v1")})
        self.assertEqual(result.updates[0]["values"]["T+核对时间"], _cells("2026-08-04 03:00"))
        self.assertEqual(result.updates[0]["values"]["父件名称"], _cells("新名"))

    def test_creates_rows_for_bom_codes_absent_from_the_sheet(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A")}}]
        creates = self._creates(records, {"A": ("甲", "v1"), "B": ("乙", "v2")})
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["values"]["父件编码"], _cells("B"))
        self.assertEqual(creates[0]["values"]["父件名称"], _cells("乙"))
        self.assertEqual(creates[0]["values"]["T+匹配状态"], _cells("一致"))
        self.assertEqual(creates[0]["values"]["T+核对时间"], _cells("2026-08-06 03:00"))

    def test_created_rows_leave_model_and_standard_columns_empty(self) -> None:
        """型号留空是人工筛选待补标准行的唯一依据，不能顺手填上。"""
        creates = self._creates([], {"B": ("乙", "v2")})
        self.assertEqual(set(creates[0]["values"]), {"父件编码", "父件名称", "T+匹配状态", "T+核对时间"})

    def test_creates_nothing_when_every_bom_code_already_has_a_row(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A")}}]
        self.assertEqual(self._creates(records, {"A": ("甲", "v1")}), [])

    def test_blank_code_rows_do_not_suppress_creation(self) -> None:
        """表里有一行只填了型号没填编码，不能因此认为 T+ 的编码已存在。"""
        records = [{"record_id": "r1", "values": {"型号": _cells("只有型号")}}]
        creates = self._creates(records, {"A": ("甲", "v1")})
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["values"]["父件编码"], _cells("A"))

    def test_creates_are_sorted_by_code_for_stable_batches(self) -> None:
        creates = self._creates([], {"C": ("丙", "v"), "A": ("甲", "v"), "B": ("乙", "v")})
        codes = [item["values"]["父件编码"][0]["text"] for item in creates]
        self.assertEqual(codes, ["A", "B", "C"])

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

    def test_alert_reports_created_rows(self) -> None:
        result = self._plan([], {})
        result.created_rows = ["A", "B"]
        text = self.module.build_alert(result)
        self.assertIn("补建 2 行", text)
        self.assertIn("A", text)
        self.assertNotIn("✅ 无异常。", text)

    def test_dry_run_never_calls_add_records(self) -> None:
        """dry-run 是确认补建量级的唯一手段，误写会直接把上千行灌进生产表。"""
        import inspect
        source = inspect.getsource(self.module.run_tplus_parent_match)
        head, _, tail = source.partition("if dry_run:")
        self.assertTrue(tail, "run_tplus_parent_match 必须保留 dry_run 分支")
        self.assertNotIn("add_records", head)

    def test_active_bom_query_excludes_superseded_versions(self) -> None:
        self.assertIn("missing_since IS NULL", self.module._ACTIVE_BOM_SQL)
        self.assertIn("DISTINCT ON (raw_json->>'Code')", self.module._ACTIVE_BOM_SQL)

    def test_managed_fields_never_include_the_parent_code(self) -> None:
        self.assertNotIn(self.module.F_PARENT_CODE, self.module.MANAGED_FIELDS)
        self.assertEqual(self.module.MANAGED_FIELDS, ("父件名称", "T+匹配状态", "T+核对时间"))


if __name__ == "__main__":
    unittest.main()
