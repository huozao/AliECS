from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _cells(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


class _FakeWeComClient:
    """记录调用顺序与参数的假企微客户端，供编排测试打桩用。"""

    def __init__(self, api_error_cls, records=None, fail_add_batches=None):
        self.api_error_cls = api_error_cls
        self.records = records or []
        self.fail_add_batches = set(fail_add_batches or [])
        self.calls: list[str] = []
        self.add_records_batches: list[list] = []
        self.update_records_batches: list[list] = []

    def get_fields(self, docid, sheet_id):
        self.calls.append("get_fields")
        return {"fields": [{"field_title": name} for name in ("父件名称", "T+匹配状态", "T+核对时间")]}

    def add_fields(self, docid, sheet_id, fields):
        self.calls.append("add_fields")

    def get_records(self, docid, sheet_id):
        self.calls.append("get_records")
        return {"records": self.records}

    def _post(self, path, payload):
        self.calls.append("_post")
        self.update_records_batches.append(payload.get("records") or [])
        return {"errcode": 0}

    def add_records(self, docid, sheet_id, records):
        self.calls.append("add_records")
        batch_no = len(self.add_records_batches) + 1
        self.add_records_batches.append(records)
        if batch_no in self.fail_add_batches:
            raise self.api_error_cls(
                "/wedoc/smartsheet/add_records",
                {"errcode": 301031, "errmsg": "boom"},
            )
        return {"errcode": 0}


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

    def _patch_run(self, *, bom, records, fail_add_batches=None):
        """打桩 run_tplus_parent_match 的外部依赖（凭据/数据源/DB/企微客户端/飞书推送）。

        注意：patch 的是 app.pipelines.tplus_parent_match 模块命名空间里的名字，
        因为 run_tplus_parent_match 内部都是裸名调用，在模块全局里查找。
        """
        fake_client = _FakeWeComClient(self.module.WeComApiError, records=records, fail_add_batches=fail_add_batches)
        credential = SimpleNamespace(corpid="c", secret="s")

        patchers = [
            patch.object(self.module, "wecom_credentials", return_value=[credential]),
            patch.object(self.module, "resolve_source", return_value=("doc1", "sheet1")),
            patch.object(self.module, "load_active_bom", return_value=bom),
            patch.object(self.module, "WeComSmartsheetClient", return_value=fake_client),
            patch.object(self.module, "send_feishu_alert"),
        ]
        mocks = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)
        mock_send_feishu_alert = mocks[-1]
        return fake_client, mock_send_feishu_alert

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

    def test_dry_run_makes_no_write_calls_behaviorally(self) -> None:
        """行为断言，防止字符串检查失效——就算 add_records 被抽进辅助函数，这条仍能拦住。"""
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        bom = {"A": ("新名", "v1"), "B": ("乙", "v2")}
        fake_client, mock_send_feishu_alert = self._patch_run(bom=bom, records=records)

        exit_code = self.module.run_tplus_parent_match(dry_run=True, notify=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_client.add_records_batches, [])
        self.assertEqual(fake_client.calls.count("_post"), 0)
        self.assertEqual(fake_client.calls.count("get_fields"), 0)
        mock_send_feishu_alert.assert_not_called()

    def test_ensure_fields_runs_before_both_write_loops(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        bom = {"A": ("新名", "v1"), "B": ("乙", "v2")}
        fake_client, _ = self._patch_run(bom=bom, records=records)

        self.module.run_tplus_parent_match(notify=False)

        self.assertIn("get_fields", fake_client.calls)
        self.assertIn("_post", fake_client.calls)
        self.assertIn("add_records", fake_client.calls)
        self.assertLess(fake_client.calls.index("get_fields"), fake_client.calls.index("_post"))
        self.assertLess(fake_client.calls.index("get_fields"), fake_client.calls.index("add_records"))

    def test_notify_false_never_calls_feishu_alert(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        bom = {"A": ("新名", "v1")}
        _, mock_send_feishu_alert = self._patch_run(bom=bom, records=records)

        self.module.run_tplus_parent_match(notify=False)

        mock_send_feishu_alert.assert_not_called()

    def test_write_batch_error_does_not_break_remaining_batches_and_still_notifies(self) -> None:
        """某批补建失败必须继续写剩余批次，而不是 break；且失败要能被人看到（推飞书）。"""
        bom = {f"CODE{i:04d}": (f"NAME{i}", "v1") for i in range(250)}  # 250 行 -> add_records 拆成 2 批（200+50）
        fake_client, mock_send_feishu_alert = self._patch_run(bom=bom, records=[], fail_add_batches={1})

        exit_code = self.module.run_tplus_parent_match(notify=True)

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(fake_client.add_records_batches), 2, "第 1 批失败后第 2 批仍应被调用，不能 break")
        mock_send_feishu_alert.assert_called_once()
        alert_text = mock_send_feishu_alert.call_args[0][0]
        self.assertIn("补建第 1 批", alert_text)

    def test_active_bom_query_excludes_superseded_versions(self) -> None:
        self.assertIn("missing_since IS NULL", self.module._ACTIVE_BOM_SQL)
        self.assertIn("DISTINCT ON (raw_json->>'Code')", self.module._ACTIVE_BOM_SQL)

    def test_managed_fields_never_include_the_parent_code(self) -> None:
        self.assertNotIn(self.module.F_PARENT_CODE, self.module.MANAGED_FIELDS)
        self.assertEqual(self.module.MANAGED_FIELDS, ("父件名称", "T+匹配状态", "T+核对时间"))


if __name__ == "__main__":
    unittest.main()
