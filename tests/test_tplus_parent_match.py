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
        return {"fields": [{"field_title": name} for name in ("父件名称", "T+匹配状态", "T+核对时间", "T+停用")]}

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


class _RecordingPlatform:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.steps: list[dict] = []
        self.finished: list[dict] = []
        self.closed = False

    def start_run(self, **kwargs):
        self.started.append(kwargs)
        return 77

    def upsert_step(self, run_id, seq, name, status, *, items=0, message=""):
        self.steps.append({
            "run_id": run_id, "seq": seq, "name": name, "status": status,
            "items": items, "message": message,
        })

    def finish_run(self, run_id, **kwargs):
        self.finished.append({"run_id": run_id, **kwargs})

    def close(self):
        self.closed = True


class _RaisingPlatform(_RecordingPlatform):
    def __init__(self, stage: str) -> None:
        super().__init__()
        self.stage = stage

    def start_run(self, **kwargs):
        if self.stage == "start":
            raise RuntimeError("platform unavailable")
        return super().start_run(**kwargs)

    def upsert_step(self, *args, **kwargs):
        if self.stage == "step":
            raise RuntimeError("platform unavailable")
        return super().upsert_step(*args, **kwargs)

    def finish_run(self, *args, **kwargs):
        if self.stage == "finish":
            raise RuntimeError("platform unavailable")
        return super().finish_run(*args, **kwargs)


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

    @staticmethod
    def _terminal_steps(platform):
        return [step for step in platform.steps if step["status"] in ("success", "failed")]

    def test_platform_records_the_fixed_parent_match_lifecycle(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        platform = _RecordingPlatform()
        self._patch_run(bom={"A": ("新名", "v1", False), "B": ("乙", "v1", False)}, records=records)

        exit_code = self.module.run_tplus_parent_match(platform=platform, trigger="schedule")

        self.assertEqual(0, exit_code)
        self.assertEqual([{
            "job_key": "tplus.parent_match", "kind": "reconcile", "provider": "chanjet",
            "display_name": "T+ 父件核对", "source_id": None, "trigger": "schedule", "legacy_ref": {},
        }], platform.started)
        self.assertEqual([
            (1, "load_source", "success", 1, ""),
            (2, "fetch_page", "success", 1, ""),
            (3, "normalize", "success", 1, ""),
            (4, "writeback", "success", 2, ""),
            (5, "notify", "success", 1, ""),
        ], [(step["seq"], step["name"], step["status"], step["items"], step["message"])
           for step in self._terminal_steps(platform)])
        self.assertEqual(("success", 1, 2), (
            platform.finished[0]["status"], platform.finished[0]["row_count"], platform.finished[0]["changed_count"],
        ))

    def test_platform_marks_wecom_read_failure_as_failed_network_without_secret(self) -> None:
        platform = _RecordingPlatform()
        credential = SimpleNamespace(corpid="c", secret="s")
        failure = RuntimeError("connection reset Authorization: Bearer secret-value")
        with patch.object(self.module, "wecom_credentials", return_value=[credential]), \
                patch.object(self.module, "resolve_source", side_effect=failure), \
                patch.object(self.module, "send_feishu_alert"):
            exit_code = self.module.run_tplus_parent_match(platform=platform)

        self.assertEqual(1, exit_code)
        failed = self._terminal_steps(platform)
        self.assertEqual((1, "load_source", "failed", 0),
                         (failed[-1]["seq"], failed[-1]["name"], failed[-1]["status"], failed[-1]["items"]))
        self.assertNotIn("secret-value", failed[-1]["message"])
        self.assertEqual("failed", platform.finished[0]["status"])
        self.assertIn("connection reset", str(platform.finished[0]["error"]))
        from app.storage.sync_job_platform import classify_error, safe_error_message
        self.assertEqual("network", classify_error(platform.finished[0]["error"]))
        self.assertNotIn("secret-value", safe_error_message(platform.finished[0]["error"]))

    def test_platform_records_partial_writeback_as_partial(self) -> None:
        platform = _RecordingPlatform()
        bom = {f"CODE{i:04d}": (f"NAME{i}", "v1", False) for i in range(250)}
        self._patch_run(bom=bom, records=[], fail_add_batches={1})

        exit_code = self.module.run_tplus_parent_match(platform=platform)

        self.assertEqual(1, exit_code)
        writeback = [step for step in self._terminal_steps(platform) if step["name"] == "writeback"]
        self.assertEqual([(4, "failed", 250, "write failure")], [
            (step["seq"], step["status"], step["items"], step["message"]) for step in writeback
        ])
        self.assertEqual("partial", platform.finished[0]["status"])
        self.assertEqual(50, platform.finished[0]["changed_count"])

    def test_dry_run_records_writeback_without_writing(self) -> None:
        platform = _RecordingPlatform()
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        self._patch_run(bom={"A": ("新名", "v1", False), "B": ("乙", "v1", False)}, records=records)

        exit_code = self.module.run_tplus_parent_match(dry_run=True, notify=True, platform=platform)

        self.assertEqual(0, exit_code)
        writeback = next(step for step in self._terminal_steps(platform) if step["name"] == "writeback")
        self.assertEqual(("success", 2, "dry-run"), (writeback["status"], writeback["items"], writeback["message"]))
        self.assertEqual(("success", 0), (platform.finished[0]["status"], platform.finished[0]["changed_count"]))

    def test_notify_disabled_is_recorded_without_sending(self) -> None:
        platform = _RecordingPlatform()
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        _, send = self._patch_run(bom={"A": ("新名", "v1", False)}, records=records)

        self.assertEqual(0, self.module.run_tplus_parent_match(notify=False, platform=platform))

        send.assert_not_called()
        notify = next(step for step in self._terminal_steps(platform) if step["name"] == "notify")
        self.assertEqual(("success", 0, "disabled"), (notify["status"], notify["items"], notify["message"]))

    def test_platform_failures_do_not_change_parent_match_result_and_owned_writer_closes(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        for stage in ("start", "step", "finish"):
            with self.subTest(stage=stage):
                self._patch_run(bom={"A": ("新名", "v1", False)}, records=records)
                self.assertEqual(0, self.module.run_tplus_parent_match(platform=_RaisingPlatform(stage)))
        owned = _RecordingPlatform()
        self._patch_run(bom={"A": ("新名", "v1", False)}, records=records)
        with patch.object(self.module, "open_owned", return_value=owned):
            self.assertEqual(0, self.module.run_tplus_parent_match())
        self.assertTrue(owned.closed)

    def test_unknown_normalize_error_fails_the_running_step_before_the_run(self) -> None:
        platform = _RecordingPlatform()
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A")}}]
        self._patch_run(bom={"A": ("甲", "v1", False)}, records=records)
        failure = ValueError("Authorization: Bearer secret-value normalize failed")

        with patch.object(self.module, "plan_updates", side_effect=failure):
            with self.assertRaisesRegex(ValueError, "normalize failed"):
                self.module.run_tplus_parent_match(platform=platform)

        terminal = self._terminal_steps(platform)
        self.assertEqual((3, "normalize", "failed", 0),
                         (terminal[-1]["seq"], terminal[-1]["name"], terminal[-1]["status"], terminal[-1]["items"]))
        self.assertNotIn("secret-value", terminal[-1]["message"])
        self.assertEqual("failed", platform.finished[-1]["status"])

    def test_owned_platform_open_and_close_failures_do_not_change_legacy_result(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        self._patch_run(bom={"A": ("新名", "v1", False)}, records=records)
        with patch.object(self.module, "open_owned", side_effect=RuntimeError("platform unavailable")):
            self.assertEqual(0, self.module.run_tplus_parent_match())

        class _CloseFailingPlatform(_RecordingPlatform):
            def close(self):
                self.closed = True
                raise RuntimeError("platform close failed")

        owned = _CloseFailingPlatform()
        self._patch_run(bom={"A": ("新名", "v1", False)}, records=records)
        with patch.object(self.module, "open_owned", return_value=owned):
            self.assertEqual(0, self.module.run_tplus_parent_match())
        self.assertTrue(owned.closed)

    def test_owned_writer_closes_when_business_error_preserves_original_exception(self) -> None:
        owned = _RecordingPlatform()
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A")}}]
        self._patch_run(bom={"A": ("甲", "v1", False)}, records=records)
        with patch.object(self.module, "open_owned", return_value=owned), \
                patch.object(self.module, "plan_updates", side_effect=ValueError("normalize failed")):
            with self.assertRaisesRegex(ValueError, "normalize failed"):
                self.module.run_tplus_parent_match()
        self.assertTrue(owned.closed)

    def test_direct_call_uses_manual_trigger_by_default(self) -> None:
        platform = _RecordingPlatform()
        self._patch_run(bom={"A": ("甲", "v1", False)}, records=[])

        self.assertEqual(0, self.module.run_tplus_parent_match(platform=platform))

        self.assertEqual("manual", platform.started[0]["trigger"])

    def test_worker_default_full_sync_uses_schedule_trigger(self) -> None:
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from app.pipelines import worker_loop

        calls: list[dict] = []
        with patch.object(worker_loop, "_maybe_start_group_listener"), \
                patch.object(worker_loop, "run_sync_wecom_full", return_value=0), \
                patch.object(worker_loop, "run_backfill_images", return_value=SimpleNamespace(
                    target_count=0, scanned_count=0, updated_count=0, error_count=0,
                )), \
                patch.object(worker_loop, "run_sync_feishu_full", return_value=0), \
                patch.object(worker_loop, "run_pending_document_locator_mirror_jobs", return_value=0), \
                patch.object(worker_loop, "run_tplus_parent_match", side_effect=lambda **kwargs: calls.append(kwargs) or 0), \
                patch.object(worker_loop, "run_pending_sync_requests", return_value=0), \
                patch.object(worker_loop, "run_write_rnd_records", return_value=0), \
                patch.object(worker_loop, "run_backfill_if_bom_synced", return_value=(None, False)):
            self.assertEqual(0, worker_loop.run_worker_loop(
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=lambda: {"enabled": True, "interval_seconds": 60, "anchor_time": ""},
                config_puller=lambda: "noop",
                now_fn=lambda: datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc),
                last_full_reader=lambda: None,
            ))

        self.assertEqual([{"trigger": "schedule"}], calls)

    def test_matched_row_fills_parent_name_from_tplus(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("40000019"), "型号": _cells("0539-ABS耐候玄武灰")}}]
        result = self._plan(records, {"40000019": ("0539-耐候ABS  玄武灰色母", "20250208", False)})
        self.assertEqual(result.ok, 1)
        self.assertEqual(result.updates[0]["values"]["父件名称"], _cells("0539-耐候ABS  玄武灰色母"))
        self.assertEqual(result.updates[0]["values"]["T+匹配状态"], _cells("一致"))

    def test_enabled_parent_writes_enabled_into_the_disabled_column(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("甲")}}]
        result = self._plan(records, {"A": ("甲", "v1", False)})
        self.assertEqual(result.updates[0]["values"]["T+停用"], _cells("启用"))

    def test_disabled_parent_writes_disabled_into_the_disabled_column(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("甲")}}]
        result = self._plan(records, {"A": ("甲", "v1", True)})
        self.assertEqual(result.updates[0]["values"]["T+停用"], _cells("停用"))

    def test_disabled_parent_is_still_a_match_not_a_missing_code(self) -> None:
        """停用 ≠ 编码没了：停用件仍在 T+ 里，不能标失联，也不该进 missing 告警。"""
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("甲")}}]
        result = self._plan(records, {"A": ("甲", "v1", True)})
        self.assertEqual(result.missing, [])
        self.assertEqual(result.ok, 1)
        self.assertEqual(result.updates[0]["values"]["T+匹配状态"], _cells("一致"))

    def test_newly_disabled_parents_are_collected_for_the_alert(self) -> None:
        records = [
            {"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("甲")}},
            {"record_id": "r2", "values": {"父件编码": _cells("B"), "父件名称": _cells("乙"), "型号": _cells("M2")}},
        ]
        result = self._plan(records, {"A": ("甲", "v1", False), "B": ("乙", "v2", True)})
        self.assertEqual(result.disabled, [("B", "M2")])

    def test_already_disabled_parents_are_not_realerted_every_run(self) -> None:
        """存量停用件每轮都在，报了就是每天一条一模一样的告警。"""
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("B"), "父件名称": _cells("乙"),
            "T+匹配状态": _cells("一致"), "T+停用": _cells("停用")}}]
        result = self._plan(records, {"B": ("乙", "v2", True)})
        self.assertEqual(result.disabled, [])

    def test_row_already_marked_disabled_produces_no_write(self) -> None:
        """新列同样走「只有真变了才写」，否则停用件每轮都被重写一次。"""
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("A"), "父件名称": _cells("甲"),
            "T+匹配状态": _cells("一致"), "T+停用": _cells("停用")}}]
        result = self._plan(records, {"A": ("甲", "v1", True)})
        self.assertEqual(result.updates, [])

    def test_disabled_state_flip_carries_the_checked_at_stamp(self) -> None:
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("A"), "父件名称": _cells("甲"),
            "T+匹配状态": _cells("一致"), "T+停用": _cells("启用")}}]
        result = self._plan(records, {"A": ("甲", "v1", True)})
        self.assertEqual(result.updates[0]["values"]["T+停用"], _cells("停用"))
        self.assertEqual(result.updates[0]["values"]["T+核对时间"], _cells("2026-08-04 03:00"))

    def test_renamed_row_is_updated_and_reported(self) -> None:
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("06000002"), "型号": _cells("乌金灰"), "父件名称": _cells("9001-cscscs")}}]
        result = self._plan(records, {"06000002": ("HYD-9721乌金灰 改性", "260720", False)})
        self.assertEqual(len(result.renamed), 1)
        self.assertEqual(result.renamed[0][2:], ("9001-cscscs", "HYD-9721乌金灰 改性"))
        self.assertEqual(result.updates[0]["values"]["T+匹配状态"], _cells("名称已更新"))

    def test_missing_code_never_rewrites_the_code_or_name(self) -> None:
        """编码是执行主键，失联时只标状态——自动改主键判错会顺着执行链扩散。"""
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("HYD-6800新"), "型号": _cells("HYD-6800墨绿"), "父件名称": _cells("HYD-6800墨绿色母")}}]
        result = self._plan(records, {"HYD-6800X": ("HYD-6800墨绿色母", "20250816", False)})
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
            "父件编码": _cells("40000019"), "父件名称": _cells("已经对了"),
            "T+匹配状态": _cells("一致"), "T+停用": _cells("启用")}}]
        result = self._plan(records, {"40000019": ("已经对了", "v1", False)})
        self.assertEqual(result.updates, [])
        self.assertEqual(result.ok, 1)

    def test_changed_row_still_carries_the_checked_at_stamp(self) -> None:
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("40000019"), "父件名称": _cells("旧名"), "T+匹配状态": _cells("一致")}}]
        result = self._plan(records, {"40000019": ("新名", "v1", False)})
        self.assertEqual(result.updates[0]["values"]["T+核对时间"], _cells("2026-08-04 03:00"))
        self.assertEqual(result.updates[0]["values"]["父件名称"], _cells("新名"))

    def test_creates_rows_for_bom_codes_absent_from_the_sheet(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A")}}]
        creates = self._creates(records, {"A": ("甲", "v1", False), "B": ("乙", "v2", False)})
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["values"]["父件编码"], _cells("B"))
        self.assertEqual(creates[0]["values"]["父件名称"], _cells("乙"))
        self.assertEqual(creates[0]["values"]["T+匹配状态"], _cells("一致"))
        self.assertEqual(creates[0]["values"]["T+核对时间"], _cells("2026-08-06 03:00"))

    def test_created_rows_leave_model_and_standard_columns_empty(self) -> None:
        """型号留空是人工筛选待补标准行的唯一依据，不能顺手填上。"""
        creates = self._creates([], {"B": ("乙", "v2", False)})
        self.assertEqual(set(creates[0]["values"]), {"父件编码", "父件名称", "T+匹配状态", "T+核对时间", "T+停用"})

    def test_created_rows_carry_the_disabled_column(self) -> None:
        creates = self._creates([], {"B": ("乙", "v2", True)})
        self.assertEqual(creates[0]["values"]["T+停用"], _cells("停用"))

    def test_creates_nothing_when_every_bom_code_already_has_a_row(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A")}}]
        self.assertEqual(self._creates(records, {"A": ("甲", "v1", False)}), [])

    def test_blank_code_rows_do_not_suppress_creation(self) -> None:
        """表里有一行只填了型号没填编码，不能因此认为 T+ 的编码已存在。"""
        records = [{"record_id": "r1", "values": {"型号": _cells("只有型号")}}]
        creates = self._creates(records, {"A": ("甲", "v1", False)})
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["values"]["父件编码"], _cells("A"))

    def test_creates_are_sorted_by_code_for_stable_batches(self) -> None:
        creates = self._creates([], {"C": ("丙", "v", False), "A": ("甲", "v", False), "B": ("乙", "v", False)})
        codes = [item["values"]["父件编码"][0]["text"] for item in creates]
        self.assertEqual(codes, ["A", "B", "C"])

    def test_alert_lists_missing_rows_and_says_code_untouched(self) -> None:
        records = [
            {"record_id": "r1", "values": {"父件编码": _cells("A"), "型号": _cells("甲")}},
            {"record_id": "r2", "values": {"父件编码": _cells("B"), "型号": _cells("乙"), "父件名称": _cells("旧名")}},
        ]
        result = self._plan(records, {"B": ("新名", "v1", False)})
        text = self.module.build_alert(result)
        self.assertIn("编码失联 1 行", text)
        self.assertIn("未自动改编码", text)
        self.assertIn("A｜甲", text)
        self.assertIn("旧名 → 新名", text)

    def test_alert_says_no_problem_when_everything_matches(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("甲")}}]
        text = self.module.build_alert(self._plan(records, {"A": ("甲", "v1", False)}))
        self.assertIn("✅ 无异常。", text)

    def test_alert_reports_disabled_parents(self) -> None:
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("B"), "型号": _cells("乙"), "父件名称": _cells("甲")}}]
        text = self.module.build_alert(self._plan(records, {"B": ("甲", "v1", True)}))
        self.assertIn("T+ 新增停用 1 行", text)
        self.assertIn("B｜乙", text)
        self.assertNotIn("✅ 无异常。", text)

    def test_alert_stays_quiet_when_nothing_is_disabled(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("甲")}}]
        text = self.module.build_alert(self._plan(records, {"A": ("甲", "v1", False)}))
        self.assertNotIn("已停用", text)

    def test_alert_reports_created_rows(self) -> None:
        result = self._plan([], {})
        result.created_rows = ["A", "B"]
        text = self.module.build_alert(result)
        self.assertIn("补建 2 行", text)
        self.assertIn("A", text)
        self.assertNotIn("✅ 无异常。", text)

    def test_dry_run_makes_no_write_calls_behaviorally(self) -> None:
        """行为断言，防止字符串检查失效——就算 add_records 被抽进辅助函数，这条仍能拦住。"""
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        bom = {"A": ("新名", "v1", False), "B": ("乙", "v2", False)}
        fake_client, mock_send_feishu_alert = self._patch_run(bom=bom, records=records)

        exit_code = self.module.run_tplus_parent_match(dry_run=True, notify=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_client.add_records_batches, [])
        self.assertEqual(fake_client.calls.count("_post"), 0)
        self.assertEqual(fake_client.calls.count("get_fields"), 0)
        mock_send_feishu_alert.assert_not_called()

    def test_ensure_fields_runs_before_both_write_loops(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        bom = {"A": ("新名", "v1", False), "B": ("乙", "v2", False)}
        fake_client, _ = self._patch_run(bom=bom, records=records)

        self.module.run_tplus_parent_match(notify=False)

        self.assertIn("get_fields", fake_client.calls)
        self.assertIn("_post", fake_client.calls)
        self.assertIn("add_records", fake_client.calls)
        self.assertLess(fake_client.calls.index("get_fields"), fake_client.calls.index("_post"))
        self.assertLess(fake_client.calls.index("get_fields"), fake_client.calls.index("add_records"))

    def test_notify_false_never_calls_feishu_alert(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A"), "父件名称": _cells("旧名")}}]
        bom = {"A": ("新名", "v1", False)}
        _, mock_send_feishu_alert = self._patch_run(bom=bom, records=records)

        self.module.run_tplus_parent_match(notify=False)

        mock_send_feishu_alert.assert_not_called()

    def test_write_batch_error_does_not_break_remaining_batches_and_still_notifies(self) -> None:
        """某批补建失败必须继续写剩余批次，而不是 break；且失败要能被人看到（推飞书）。"""
        bom = {f"CODE{i:04d}": (f"NAME{i}", "v1", False) for i in range(250)}  # 250 行 -> add_records 拆成 2 批（200+50）
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

    def test_both_active_queries_select_the_disabled_flag(self) -> None:
        """停用状态直接取自 T+ 原始行，不另建映射表。"""
        self.assertIn("'Disabled'", self.module._ACTIVE_BOM_SQL)
        self.assertIn("'Disabled'", self.module._ACTIVE_INVENTORY_SQL)

    def test_active_inventory_query_covers_inventory_only_parents(self) -> None:
        self.assertIn("tplus_inventory_records", self.module._ACTIVE_INVENTORY_SQL)
        self.assertIn("missing_since IS NULL", self.module._ACTIVE_INVENTORY_SQL)
        self.assertIn("raw_json->>'Code'", self.module._ACTIVE_INVENTORY_SQL)
        self.assertIn("InventoryClass", self.module._ACTIVE_INVENTORY_SQL)
        self.assertIn("'06'", self.module._ACTIVE_INVENTORY_SQL)
    def test_managed_fields_never_include_the_parent_code(self) -> None:
        self.assertNotIn(self.module.F_PARENT_CODE, self.module.MANAGED_FIELDS)
        self.assertEqual(self.module.MANAGED_FIELDS, ("父件名称", "T+匹配状态", "T+核对时间", "T+停用"))

    def test_bom_watermark_sql_covers_both_real_writers(self) -> None:
        """integration_sync_runs 有两个真实写入点：module='bom'（BOM builder 回写，
        finish_bom_request()）与 module='all'（每日 T+ 全量，record_tplus_sync_run_if_configured()）。
        只认 'bom' 会漏掉最常见的新父件来源——直接在 T+ 建物料/BOM 走的是全量。"""
        sql = self.module._LATEST_BOM_SYNC_SQL
        self.assertIn("integration_sync_runs", sql)
        self.assertIn("provider = 'chanjet'", sql)
        self.assertIn("module IN ('all', 'bom')", sql)
        # status 必须过滤：放开到 'all' 后，一次部分成功的全量会让未出现的记录被标 missing_since，
        # 此时触发核对会把大量行误标「编码失联」并发一条大告警。
        self.assertIn("status = 'success'", sql)

    def test_first_poll_only_records_the_watermark(self) -> None:
        """首轮不跑：容器重启风暴不该反复触发补建，当天的兜底轮已覆盖。"""
        from datetime import datetime, timezone
        stamp = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)
        calls = []
        self.module.latest_bom_sync_at = lambda: stamp
        self.module.run_tplus_parent_match = lambda **kwargs: calls.append(kwargs) or 0
        watermark, ran = self.module.run_backfill_if_bom_synced(None)
        self.assertEqual(watermark, stamp)
        self.assertFalse(ran)
        self.assertEqual(calls, [])

    def test_rising_watermark_triggers_one_backfill(self) -> None:
        from datetime import datetime, timedelta, timezone
        old = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)
        new = old + timedelta(minutes=5)
        calls = []
        self.module.latest_bom_sync_at = lambda: new
        self.module.run_tplus_parent_match = lambda **kwargs: calls.append(kwargs) or 0
        watermark, ran = self.module.run_backfill_if_bom_synced(old)
        self.assertEqual(watermark, new)
        self.assertTrue(ran)
        self.assertEqual([{"trigger": "event"}], calls)

    def test_flat_watermark_does_not_retrigger(self) -> None:
        from datetime import datetime, timezone
        stamp = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)
        calls = []
        self.module.latest_bom_sync_at = lambda: stamp
        self.module.run_tplus_parent_match = lambda **kwargs: calls.append(kwargs) or 0
        watermark, ran = self.module.run_backfill_if_bom_synced(stamp)
        self.assertEqual(watermark, stamp)
        self.assertFalse(ran)
        self.assertEqual(calls, [])

    def test_unreadable_watermark_keeps_the_old_one_and_does_not_run(self) -> None:
        """DB 读不到时保持原水位：清成 None 会让下一轮把首轮逻辑再走一遍，白跑一次。"""
        from datetime import datetime, timezone
        stamp = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)
        calls = []
        self.module.latest_bom_sync_at = lambda: None
        self.module.run_tplus_parent_match = lambda **kwargs: calls.append(kwargs) or 0
        watermark, ran = self.module.run_backfill_if_bom_synced(stamp)
        self.assertEqual(watermark, stamp)
        self.assertFalse(ran)
        self.assertEqual(calls, [])

    def test_wecom_read_failure_before_writes_notifies_and_returns_error(self) -> None:
        """resolve_source() 等读侧步骤整轮故障（token 失效/权限收回/网络不通）必须能被人看到，
        不能直接冒泡跳过 notify——那样飞书一条告警都没有，只剩容器 stdout。"""
        credential = SimpleNamespace(corpid="c", secret="s")
        with patch.object(self.module, "wecom_credentials", return_value=[credential]), \
                patch.object(self.module, "resolve_source", side_effect=RuntimeError("企微 token 失效")) as mock_resolve, \
                patch.object(self.module, "send_feishu_alert") as mock_send:
            exit_code = self.module.run_tplus_parent_match(notify=True)

        mock_resolve.assert_called_once()
        self.assertEqual(exit_code, 1)
        mock_send.assert_called_once()
        alert_text = mock_send.call_args[0][0]
        self.assertIn("核对未能开始", alert_text)
        self.assertNotIn("写入失败", alert_text)

    def test_wecom_get_records_failure_notifies_and_skips_writes(self) -> None:
        """get_records() 命中网络/权限故障时，同一层必须捕获，且不能走到写入分支。"""
        class _FailingClient:
            def get_fields(self, docid, sheet_id):
                return {"fields": [{"field_title": n} for n in ("父件名称", "T+匹配状态", "T+核对时间", "T+停用")]}

            def add_fields(self, docid, sheet_id, fields):
                raise AssertionError("不应走到 add_fields")

            def get_records(self, docid, sheet_id):
                raise RuntimeError("connection reset")

            def add_records(self, docid, sheet_id, records):
                raise AssertionError("读侧失败后不能写入")

            def _post(self, path, payload):
                raise AssertionError("读侧失败后不能写入")

        credential = SimpleNamespace(corpid="c", secret="s")
        with patch.object(self.module, "wecom_credentials", return_value=[credential]), \
                patch.object(self.module, "resolve_source", return_value=("doc1", "sheet1")), \
                patch.object(self.module, "load_active_bom", return_value={"A": ("甲", "v1", False)}), \
                patch.object(self.module, "WeComSmartsheetClient", return_value=_FailingClient()), \
                patch.object(self.module, "send_feishu_alert") as mock_send:
            exit_code = self.module.run_tplus_parent_match(notify=True)

        self.assertEqual(exit_code, 1)
        mock_send.assert_called_once()

    def test_wecom_read_failure_with_notify_false_still_returns_error_but_no_alert(self) -> None:
        credential = SimpleNamespace(corpid="c", secret="s")
        with patch.object(self.module, "wecom_credentials", return_value=[credential]), \
                patch.object(self.module, "resolve_source", side_effect=RuntimeError("boom")), \
                patch.object(self.module, "send_feishu_alert") as mock_send:
            exit_code = self.module.run_tplus_parent_match(notify=False)

        self.assertEqual(exit_code, 1)
        mock_send.assert_not_called()

    def test_feishu_alert_delivery_failure_forces_nonzero_exit_code(self) -> None:
        """send_feishu_alert() 吞异常返回 False 时，调用方不能把返回值丢掉——否则写失败+告警失败
        同时发生时这一轮补建完全无声，要等次日兜底轮。"""
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A")}}]
        bom = {"A": ("甲", "v1", False), "B": ("乙", "v2", False)}  # B 触发 created_rows，进而触发 notify 分支
        fake_client, mock_send_feishu_alert = self._patch_run(bom=bom, records=records)
        mock_send_feishu_alert.return_value = False

        exit_code = self.module.run_tplus_parent_match(notify=True)

        self.assertEqual(exit_code, 1)
        mock_send_feishu_alert.assert_called_once()

    def test_backfill_advances_watermark_even_when_run_fails(self) -> None:
        """写批次失败（exit_code!=0）不能让事件通道当天不再重试之外，也不能变成 30s 告警风暴——
        水位仍要推进，失败可见性由 run_tplus_parent_match 自己的 notify 负责。"""
        from datetime import datetime, timedelta, timezone
        old = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)
        new = old + timedelta(minutes=5)
        self.module.latest_bom_sync_at = lambda: new
        self.module.run_tplus_parent_match = lambda **kwargs: 1
        watermark, ran = self.module.run_backfill_if_bom_synced(old)
        self.assertEqual(watermark, new)
        self.assertTrue(ran)

    def test_write_batch_runtime_error_does_not_break_remaining_batches_and_still_notifies(self) -> None:
        """企微客户端网络层失败抛裸 RuntimeError（非 WeComApiError），同样不能中断批次或吞掉告警。"""
        bom = {f"CODE{i:04d}": (f"NAME{i}", "v1", False) for i in range(250)}  # 250 行 -> add_records 拆成 2 批（200+50）
        fake_client, mock_send_feishu_alert = self._patch_run(bom=bom, records=[], fail_add_batches=set())
        original_add_records = fake_client.add_records

        def _add_records_raw_runtime_error(docid, sheet_id, records):
            batch_no = len(fake_client.add_records_batches) + 1
            if batch_no == 1:
                fake_client.calls.append("add_records")
                fake_client.add_records_batches.append(records)
                raise RuntimeError("connection reset")
            return original_add_records(docid, sheet_id, records)

        fake_client.add_records = _add_records_raw_runtime_error

        exit_code = self.module.run_tplus_parent_match(notify=True)

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(fake_client.add_records_batches), 2, "第 1 批裸 RuntimeError 后第 2 批仍应被调用，不能 break")
        mock_send_feishu_alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
