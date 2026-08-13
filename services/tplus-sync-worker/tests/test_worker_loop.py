import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tplus_datahub.jobs.worker_loop import run_forever


class WorkerLoopTests(unittest.TestCase):
    UTC = timezone.utc

    @staticmethod
    def _schedule(*, enabled=True, interval_seconds=60, anchor_time=""):
        return {
            "enabled": enabled,
            "interval_seconds": interval_seconds,
            "anchor_time": anchor_time,
        }

    def test_run_forever_runs_immediately_then_sleeps_between_runs(self):
        calls = []
        sleeps = []
        old_interval = os.environ.get("TPLUS_SYNC_INTERVAL_SECONDS")
        os.environ["TPLUS_SYNC_INTERVAL_SECONDS"] = "7"
        try:
            result = run_forever(
                sync_once=lambda: calls.append("sync") or 0,
                sleep=sleeps.append,
                max_runs=2,
            )
        finally:
            if old_interval is None:
                os.environ.pop("TPLUS_SYNC_INTERVAL_SECONDS", None)
            else:
                os.environ["TPLUS_SYNC_INTERVAL_SECONDS"] = old_interval

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["sync", "sync"])
        self.assertEqual(sleeps, [7])

    def test_run_forever_returns_last_sync_exit_code_for_bounded_runs(self):
        exit_codes = iter([0, 3])
        result = run_forever(
            sync_once=lambda: next(exit_codes),
            sleep=lambda _seconds: None,
            max_runs=2,
        )

        self.assertEqual(result, 3)

    def test_run_forever_defaults_to_daily_full_reconciliation_interval(self):
        old_interval = os.environ.get("TPLUS_SYNC_INTERVAL_SECONDS")
        sleeps = []
        try:
            os.environ.pop("TPLUS_SYNC_INTERVAL_SECONDS", None)
            result = run_forever(
                sync_once=lambda: 0,
                sleep=sleeps.append,
                max_runs=2,
            )
        finally:
            if old_interval is None:
                os.environ.pop("TPLUS_SYNC_INTERVAL_SECONDS", None)
            else:
                os.environ["TPLUS_SYNC_INTERVAL_SECONDS"] = old_interval

        self.assertEqual(result, 0)
        self.assertEqual(86400, sum(sleeps))

    def test_run_forever_records_full_sync_run_status(self):
        recorded = []

        result = run_forever(
            sync_once=lambda: 0,
            record_sync_run=lambda **kwargs: recorded.append(kwargs),
            sleep=lambda _seconds: None,
            max_runs=1,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            [
                {
                    "module": "all",
                    "mode": "scheduled_full",
                    "status": "success",
                    "row_count": 0,
                    "exit_code": 0,
                    "platform_run_id": None,
                    "detail_json": {"run": 1, "export_files": [], "diff_summary": None, "full_snapshot_id": None, "failed_modules": []},
                    "error_json": {},
                }
            ],
            recorded,
        )

    def test_run_forever_records_failed_full_sync_run_status(self):
        recorded = []

        def fail_sync():
            raise RuntimeError("boom")

        result = run_forever(
            sync_once=fail_sync,
            record_sync_run=lambda **kwargs: recorded.append(kwargs),
            sleep=lambda _seconds: None,
            max_runs=1,
        )

        self.assertEqual(result, 1)
        self.assertEqual("failed", recorded[0]["status"])
        self.assertEqual(1, recorded[0]["exit_code"])
        self.assertEqual({"run": 1, "export_files": [], "diff_summary": None, "full_snapshot_id": None, "failed_modules": []}, recorded[0]["detail_json"])

    def test_worker_attaches_scheduled_legacy_run(self):
        from tplus_datahub.jobs.job_sync_all import SyncAllResult

        recorded = {}
        run_forever(
            sync_once=lambda: SyncAllResult(0, platform_run_id=77),
            record_sync_run=lambda **kwargs: recorded.update(kwargs) or 41,
            sleep=lambda _seconds: None,
            max_runs=1,
        )

        self.assertEqual(77, recorded["platform_run_id"])

    def test_default_full_sync_uses_schedule_then_manual_triggers(self):
        import tplus_datahub.jobs.worker_loop as worker_loop
        from tplus_datahub.jobs.job_sync_all import SyncAllResult

        triggers = []
        requests = [{"id": 9, "mode": "manual_full", "target_json": {}}]
        old_interval = os.environ.get("TPLUS_SYNC_INTERVAL_SECONDS")
        old_poll = os.environ.get("TPLUS_SYNC_POLL_SECONDS")
        os.environ["TPLUS_SYNC_INTERVAL_SECONDS"] = "1"
        os.environ["TPLUS_SYNC_POLL_SECONDS"] = "1"
        try:
            with patch.object(
                worker_loop,
                "sync_all_run",
                side_effect=lambda *, trigger: triggers.append(trigger) or SyncAllResult(0),
            ) as default_sync:
                run_forever(
                    sync_once=default_sync,
                    fetch_db_full_request=lambda limit=5: requests.pop(0) if requests else None,
                    finish_db_full_request=lambda *_args: None,
                    record_sync_run=lambda **_kwargs: 41,
                    sleep=lambda _seconds: None,
                    max_runs=2,
                )
        finally:
            for key, value in {
                "TPLUS_SYNC_INTERVAL_SECONDS": old_interval,
                "TPLUS_SYNC_POLL_SECONDS": old_poll,
            }.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(["schedule", "manual", "schedule"], triggers)

    def test_run_forever_records_failed_modules_for_partial_success(self):
        """模块独立容错后，整轮可能 status=success 却有模块没数据；
        backend 的告警只认 detail_json.failed_modules，这里不落就永远报不出来。"""
        recorded = []

        class _Outcome:
            exit_code = 0
            export_files = ["bom.xlsx"]
            diff_summary = None
            full_snapshot_id = None
            failed_modules = ["inventory"]

        run_forever(
            sync_once=lambda: _Outcome(),
            record_sync_run=lambda **kwargs: recorded.append(kwargs),
            sleep=lambda _seconds: None,
            max_runs=1,
        )

        self.assertEqual(["inventory"], recorded[0]["detail_json"]["failed_modules"])

    def test_run_forever_records_failure_details_in_error_json(self):
        """error_json 一直是空 {}，失败详情只能翻容器内日志文件——
        08-09 那次要不是手动复现，30 秒读超时这个决定性事实根本看不到。"""
        recorded = []

        class _Outcome:
            exit_code = 3
            export_files = []
            diff_summary = None
            full_snapshot_id = None
            failed_modules = ["bom"]
            failure_details = [{"module": "bom", "type": "ChanjetAPIError",
                                "endpoint": "/tplus/api/v2/bom/QueryPage", "status": None,
                                "message": "请求失败：Read timed out. (read timeout=30)"}]

        run_forever(
            sync_once=lambda: _Outcome(),
            record_sync_run=lambda **kwargs: recorded.append(kwargs),
            sleep=lambda _seconds: None,
            max_runs=1,
        )

        error_json = recorded[0]["error_json"]
        self.assertEqual(["bom"], [item["module"] for item in error_json["modules"]])
        self.assertIn("read timeout=30", error_json["modules"][0]["message"])

    def test_run_forever_keeps_error_json_empty_on_success(self):
        recorded = []
        run_forever(
            sync_once=lambda: 0,
            record_sync_run=lambda **kwargs: recorded.append(kwargs),
            sleep=lambda _seconds: None,
            max_runs=1,
        )
        self.assertEqual({}, recorded[0]["error_json"])

    def test_run_forever_consumes_manual_bom_request_between_full_runs(self):
        old_interval = os.environ.get("TPLUS_SYNC_INTERVAL_SECONDS")
        old_poll = os.environ.get("TPLUS_SYNC_POLL_SECONDS")
        old_request_dir = os.environ.get("TPLUS_BOM_SYNC_REQUEST_DIR")
        calls = []
        sleeps = []
        with tempfile.TemporaryDirectory() as tmp:
            request_dir = Path(tmp)
            (request_dir / "manual-bom.json").write_text('{"module":"bom","include_disabled":true}', encoding="utf-8")
            os.environ["TPLUS_SYNC_INTERVAL_SECONDS"] = "2"
            os.environ["TPLUS_SYNC_POLL_SECONDS"] = "1"
            os.environ["TPLUS_BOM_SYNC_REQUEST_DIR"] = str(request_dir)
            try:
                result = run_forever(
                    sync_once=lambda: calls.append("full") or 0,
                    sync_bom_once=lambda: calls.append("bom") or 0,
                    sleep=sleeps.append,
                    max_runs=2,
                )
            finally:
                for key, value in {
                    "TPLUS_SYNC_INTERVAL_SECONDS": old_interval,
                    "TPLUS_SYNC_POLL_SECONDS": old_poll,
                    "TPLUS_BOM_SYNC_REQUEST_DIR": old_request_dir,
                }.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            self.assertEqual(result, 0)
            self.assertEqual(calls, ["full", "bom", "full"])
            self.assertEqual(sleeps, [1, 1])
            self.assertFalse((request_dir / "manual-bom.json").exists())
            self.assertTrue((request_dir / "manual-bom.json.done").exists())

    def test_run_forever_consumes_db_bom_request_between_full_runs(self):
        old_interval = os.environ.get("TPLUS_SYNC_INTERVAL_SECONDS")
        old_poll = os.environ.get("TPLUS_SYNC_POLL_SECONDS")
        old_db_enabled = os.environ.get("TPLUS_DB_SYNC_REQUESTS_ENABLED")
        calls = []
        sleeps = []
        requests = [{"id": 7, "mode": "incremental", "target_json": {"parent_code": "HYD-4197PC"}}]

        def fake_fetch(limit=5):
            return requests.pop(0) if requests else None

        def fake_finish(request_id, status, exit_code, detail):
            calls.append(("finish", request_id, status, exit_code, detail["mode"]))

        os.environ["TPLUS_SYNC_INTERVAL_SECONDS"] = "2"
        os.environ["TPLUS_SYNC_POLL_SECONDS"] = "1"
        os.environ["TPLUS_DB_SYNC_REQUESTS_ENABLED"] = "true"
        try:
            result = run_forever(
                sync_once=lambda: calls.append(("full",)) or 0,
                sync_bom_request_once=lambda request: calls.append(("bom", request["id"], request["mode"])) or 0,
                fetch_db_bom_request=fake_fetch,
                finish_db_bom_request=fake_finish,
                sleep=sleeps.append,
                max_runs=2,
            )
        finally:
            for key, value in {
                "TPLUS_SYNC_INTERVAL_SECONDS": old_interval,
                "TPLUS_SYNC_POLL_SECONDS": old_poll,
                "TPLUS_DB_SYNC_REQUESTS_ENABLED": old_db_enabled,
            }.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ("full",))
        self.assertEqual(calls[1], ("bom", 7, "incremental"))
        self.assertEqual(calls[2], ("finish", 7, "success", 0, "incremental"))
        self.assertEqual(calls[3], ("full",))

    def test_run_forever_consumes_db_full_request_between_scheduled_runs(self):
        """页面「立即全量同步」排的队，worker 应在睡眠轮询里捡起来，跑的是同一个全量。"""
        old_interval = os.environ.get("TPLUS_SYNC_INTERVAL_SECONDS")
        old_poll = os.environ.get("TPLUS_SYNC_POLL_SECONDS")
        old_db_enabled = os.environ.get("TPLUS_DB_SYNC_REQUESTS_ENABLED")
        calls = []
        requests = [{"id": 9, "mode": "manual_full", "target_json": {}}]

        def fake_fetch_full(limit=5):
            return requests.pop(0) if requests else None

        def fake_finish_full(request_id, status, exit_code, detail):
            calls.append(("finish_full", request_id, status, exit_code))

        os.environ["TPLUS_SYNC_INTERVAL_SECONDS"] = "2"
        os.environ["TPLUS_SYNC_POLL_SECONDS"] = "1"
        os.environ["TPLUS_DB_SYNC_REQUESTS_ENABLED"] = "true"
        try:
            result = run_forever(
                sync_once=lambda: calls.append(("full",)) or 0,
                fetch_db_full_request=fake_fetch_full,
                finish_db_full_request=fake_finish_full,
                sleep=lambda _seconds: None,
                max_runs=2,
            )
        finally:
            for key, value in {
                "TPLUS_SYNC_INTERVAL_SECONDS": old_interval,
                "TPLUS_SYNC_POLL_SECONDS": old_poll,
                "TPLUS_DB_SYNC_REQUESTS_ENABLED": old_db_enabled,
            }.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(result, 0)
        # 定时全量 → 睡眠中捡到手动请求 → 再跑一次同样的全量 → 记账 → 下一轮定时全量
        self.assertEqual(calls[0], ("full",))
        self.assertEqual(calls[1], ("full",))
        self.assertEqual(calls[2], ("finish_full", 9, "success", 0))
        self.assertEqual(calls[3], ("full",))

    def test_failed_manual_full_is_recorded_as_failed(self):
        old_poll = os.environ.get("TPLUS_SYNC_POLL_SECONDS")
        old_interval = os.environ.get("TPLUS_SYNC_INTERVAL_SECONDS")
        calls = []
        requests = [{"id": 9, "mode": "manual_full", "target_json": {}}]
        os.environ["TPLUS_SYNC_INTERVAL_SECONDS"] = "2"
        os.environ["TPLUS_SYNC_POLL_SECONDS"] = "1"
        try:
            run_forever(
                sync_once=lambda: 3,
                fetch_db_full_request=lambda limit=5: requests.pop(0) if requests else None,
                finish_db_full_request=lambda rid, status, code, detail: calls.append((status, code)),
                sleep=lambda _seconds: None,
                max_runs=2,  # max_runs=1 在进入睡眠轮询前就 return，请求根本不会被消费
            )
        finally:
            for key, value in {"TPLUS_SYNC_INTERVAL_SECONDS": old_interval,
                               "TPLUS_SYNC_POLL_SECONDS": old_poll}.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(calls, [("failed", 3)])


    def test_scheduled_run_records_export_files_from_result(self):
        import tplus_datahub.jobs.worker_loop as wl
        from tplus_datahub.jobs.job_sync_all import SyncAllResult
        recorded = {}
        def fake_record(**kwargs):
            recorded.update(kwargs); return 1
        wl.run_forever(
            sync_once=lambda: SyncAllResult(0, ["bom_20260624_100751.xlsx", "current_stock_x.xlsx"]),
            record_sync_run=fake_record,
            sleep=lambda s: None,
            max_runs=1,
        )
        self.assertEqual(["bom_20260624_100751.xlsx", "current_stock_x.xlsx"],
                         recorded["detail_json"]["export_files"])

    def test_scheduled_run_records_diff_summary_and_snapshot_id(self):
        import tplus_datahub.jobs.worker_loop as wl
        from tplus_datahub.jobs.job_sync_all import SyncAllResult
        recorded = {}
        wl.run_forever(
            sync_once=lambda: SyncAllResult(0, ["bom_x.xlsx"], diff_summary={"needs_review": True, "qty_changed": 2}, full_snapshot_id=99),
            record_sync_run=lambda **k: recorded.update(k) or 1,
            sleep=lambda s: None, max_runs=1)
        self.assertEqual({"needs_review": True, "qty_changed": 2}, recorded["detail_json"]["diff_summary"])
        self.assertEqual(99, recorded["detail_json"]["full_snapshot_id"])

    def test_scheduled_run_tolerates_int_sync_once(self):
        import tplus_datahub.jobs.worker_loop as wl
        recorded = {}
        wl.run_forever(sync_once=lambda: 0, record_sync_run=lambda **k: recorded.update(k) or 1,
                       sleep=lambda s: None, max_runs=1)
        self.assertEqual([], recorded["detail_json"]["export_files"])


    def test_run_forever_skips_full_sync_when_disabled(self):
        calls = []
        sleeps = []
        result = run_forever(
            sync_once=lambda: calls.append("sync") or 0,
            record_sync_run=lambda **k: calls.append("record"),
            read_sync_config=lambda: {"enabled": False, "interval_seconds": 5},
            sleep=sleeps.append,
            max_runs=2,
        )
        self.assertEqual(result, 0)
        self.assertEqual(calls, [])      # 关掉后不跑定时全量、不记 run
        self.assertEqual(sleeps, [5])    # 间隔仍取配置值（手动轮询照常）

    def test_run_forever_uses_interval_from_config_hot(self):
        sleeps = []
        run_forever(
            sync_once=lambda: 0,
            record_sync_run=lambda **k: None,
            read_sync_config=lambda: {"enabled": True, "interval_seconds": 11},
            sleep=sleeps.append,
            max_runs=2,
        )
        self.assertEqual(sleeps, [11])   # 间隔热生效，无需重启

    def test_run_forever_falls_back_to_env_when_config_errors(self):
        old = os.environ.get("TPLUS_SYNC_INTERVAL_SECONDS")
        os.environ["TPLUS_SYNC_INTERVAL_SECONDS"] = "9"
        calls = []
        sleeps = []

        def boom():
            raise RuntimeError("db down")

        try:
            run_forever(
                sync_once=lambda: calls.append("sync") or 0,
                record_sync_run=lambda **k: None,
                read_sync_config=boom,  # 读配置抛错 → 回退默认（enabled + env 间隔）
                sleep=sleeps.append,
                max_runs=2,
            )
        finally:
            if old is None:
                os.environ.pop("TPLUS_SYNC_INTERVAL_SECONDS", None)
            else:
                os.environ["TPLUS_SYNC_INTERVAL_SECONDS"] = old
        self.assertEqual(calls, ["sync", "sync"])  # 回退后仍开启
        self.assertEqual(sleeps, [9])              # 用 env 默认间隔

    def test_shadow_with_no_anchor_keeps_legacy_restart_full_run_and_records_candidate_difference(self):
        """若把 shadow 改成共享核的无锚点语义，重启会漏掉现有立即全量。"""
        current = datetime(2026, 8, 13, 0, 0, tzinfo=self.UTC)
        scheduled = self._schedule()
        calls = []
        recorded_shadow = []

        run_forever(
            sync_once=lambda: calls.append("scheduled-full") or 0,
            record_sync_run=lambda **_kwargs: 41,
            read_last_full=lambda: current - timedelta(seconds=30),
            read_sync_config=lambda: scheduled,
            scheduler_mode_reader=lambda: "shadow",
            platform_schedule_reader=lambda: scheduled,
            shadow_recorder=lambda payload: recorded_shadow.append(payload) or [901],
            shadow_finisher=lambda *_args: None,
            now=lambda: current,
            sleep=lambda _seconds: None,
            max_runs=1,
        )

        self.assertEqual(["scheduled-full"], calls)
        self.assertEqual(1, len(recorded_shadow))
        self.assertFalse(recorded_shadow[0]["decision_match"])
        self.assertTrue(recorded_shadow[0]["legacy"]["run_full"])
        self.assertFalse(recorded_shadow[0]["candidate"]["run_full"])

    def test_shadow_with_production_anchor_matches_without_changing_execution(self):
        current = datetime(2026, 8, 13, 16, 0, tzinfo=self.UTC)
        scheduled = self._schedule(interval_seconds=86400, anchor_time="02:00")
        recorded_shadow = []
        sleeps = []

        run_forever(
            sync_once=lambda: self.fail("anchored run must remain pending"),
            read_last_full=lambda: datetime(2026, 8, 13, 14, 0, tzinfo=self.UTC),
            read_sync_config=lambda: scheduled,
            scheduler_mode_reader=lambda: "shadow",
            platform_schedule_reader=lambda: scheduled,
            shadow_recorder=lambda payload: recorded_shadow.append(payload) or [902],
            shadow_finisher=lambda *_args: None,
            now=lambda: current,
            sleep=sleeps.append,
            max_runs=2,
        )

        self.assertEqual(7200, sum(sleeps))
        self.assertEqual(True, recorded_shadow[0]["decision_match"])
        self.assertEqual(0.0, recorded_shadow[0]["due_delta_seconds"])

    def test_active_uses_candidate_doc_semantics_for_no_anchor_restart(self):
        current = datetime(2026, 8, 13, 0, 0, tzinfo=self.UTC)
        scheduled = self._schedule()
        calls = []

        run_forever(
            sync_once=lambda: calls.append("scheduled-full") or 0,
            read_last_full=lambda: current - timedelta(seconds=30),
            read_sync_config=lambda: scheduled,
            scheduler_mode_reader=lambda: "active",
            platform_schedule_reader=lambda: scheduled,
            now=lambda: current,
            sleep=lambda _seconds: None,
            max_runs=1,
        )

        self.assertEqual([], calls)

    def test_active_hot_wakes_when_candidate_target_moves_earlier(self):
        current = datetime(2026, 8, 13, 0, 0, tzinfo=self.UTC)
        platform_schedules = iter([
            self._schedule(interval_seconds=120),
            self._schedule(interval_seconds=60),
            self._schedule(interval_seconds=60),
        ])
        sleeps = []

        run_forever(
            sync_once=lambda: self.fail("candidate is still pending after replan"),
            read_last_full=lambda: current,
            read_sync_config=lambda: self._schedule(),
            scheduler_mode_reader=lambda: "active",
            platform_schedule_reader=lambda: next(platform_schedules),
            now=lambda: current,
            sleep=sleeps.append,
            max_runs=2,
        )

        self.assertEqual([30], sleeps)

    def test_active_platform_failure_after_full_falls_back_to_legacy_full_wait(self):
        """平台读故障不能把 T+ 无锚点 legacy 的一整周期 wait 变成 zero-wait 重跑。"""
        current = datetime(2026, 8, 13, 0, 0, tzinfo=self.UTC)
        reader_calls = 0
        sleeps = []

        def read_platform():
            nonlocal reader_calls
            reader_calls += 1
            if reader_calls == 1:
                return self._schedule()
            raise RuntimeError("platform unavailable")

        run_forever(
            sync_once=lambda: 0,
            read_last_full=lambda: None,
            read_sync_config=lambda: self._schedule(),
            scheduler_mode_reader=lambda: "active",
            platform_schedule_reader=read_platform,
            now=lambda: current,
            sleep=sleeps.append,
            max_runs=2,
        )

        self.assertEqual([30, 30], sleeps)

    def test_shadow_writer_failure_is_fail_open_and_keeps_legacy_full(self):
        current = datetime(2026, 8, 13, 0, 0, tzinfo=self.UTC)
        calls = []

        run_forever(
            sync_once=lambda: calls.append("scheduled-full") or 0,
            read_last_full=lambda: current - timedelta(seconds=30),
            read_sync_config=lambda: self._schedule(),
            scheduler_mode_reader=lambda: "shadow",
            platform_schedule_reader=lambda: self._schedule(),
            shadow_recorder=lambda _payload: (_ for _ in ()).throw(RuntimeError("shadow db down")),
            now=lambda: current,
            sleep=lambda _seconds: None,
            max_runs=1,
        )

        self.assertEqual(["scheduled-full"], calls)

    def test_legacy_mode_does_not_touch_platform_scheduler_or_change_earlier_anchor_wake(self):
        """这是 T+ 既有 hot-wake；legacy 模式不得因接线而读 platform。"""
        current = datetime(2026, 8, 13, 0, 0, tzinfo=self.UTC)
        configs = iter([
            self._schedule(interval_seconds=86400, anchor_time="02:00"),
            self._schedule(interval_seconds=86400, anchor_time="01:00"),
            self._schedule(interval_seconds=86400, anchor_time="01:00"),
        ])
        sleeps = []

        run_forever(
            sync_once=lambda: self.fail("earlier anchor stays pending at midnight"),
            read_last_full=lambda: datetime(2026, 8, 12, 22, 0, tzinfo=self.UTC),
            read_sync_config=lambda: next(configs),
            scheduler_mode_reader=lambda: "legacy",
            platform_schedule_reader=lambda: self.fail("legacy must not read platform schedule"),
            shadow_recorder=lambda _payload: self.fail("legacy must not record shadow"),
            shadow_finisher=lambda *_args: self.fail("legacy must not finish shadow"),
            now=lambda: current,
            sleep=sleeps.append,
            max_runs=2,
        )

        self.assertEqual([30], sleeps)

    def test_shadow_seeds_only_missing_platform_schedule_from_legacy_config(self):
        current = datetime(2026, 8, 13, 16, 0, tzinfo=self.UTC)
        scheduled = self._schedule(interval_seconds=86400, anchor_time="02:00")
        seeded = []

        run_forever(
            sync_once=lambda: self.fail("anchored run must remain pending"),
            read_last_full=lambda: datetime(2026, 8, 13, 14, 0, tzinfo=self.UTC),
            read_sync_config=lambda: scheduled,
            scheduler_mode_reader=lambda: "shadow",
            platform_schedule_reader=lambda: None,
            platform_schedule_seeder=seeded.append,
            now=lambda: current,
            sleep=lambda _seconds: None,
            max_runs=1,
        )

        self.assertEqual([scheduled], seeded)

    def test_shadow_finalizes_exact_ids_with_monotonic_sleep_and_observed_early_wake(self):
        current = datetime(2026, 8, 13, 0, 0, tzinfo=self.UTC)
        scheduled = self._schedule()
        platform_schedules = iter([
            self._schedule(interval_seconds=120),
            self._schedule(interval_seconds=120),
            self._schedule(interval_seconds=60),
            self._schedule(interval_seconds=60),
        ])
        monotonic_values = iter([0.0, 7.0, 8.0, 15.0])
        finished = []
        sleeps = []

        run_forever(
            sync_once=lambda: 0,
            record_sync_run=lambda **_kwargs: 41,
            read_sync_config=lambda: scheduled,
            scheduler_mode_reader=lambda: "shadow",
            platform_schedule_reader=lambda: next(platform_schedules),
            shadow_recorder=lambda _payload: [731, 932],
            shadow_finisher=lambda run_ids, observed, would_wake: finished.append((run_ids, observed, would_wake)),
            monotonic=lambda: next(monotonic_values),
            now=lambda: current,
            sleep=sleeps.append,
            max_runs=2,
        )

        self.assertEqual([30, 30], sleeps)
        self.assertEqual([([731, 932], 14, True)], finished)

    def test_shadow_keeps_manual_bom_and_full_request_polls_in_every_legacy_slice(self):
        current = datetime(2026, 8, 13, 0, 0, tzinfo=self.UTC)
        scheduled = self._schedule()
        requests = [{"id": 9, "mode": "manual_full", "target_json": {}}]
        calls = []
        old_poll = os.environ.get("TPLUS_SYNC_POLL_SECONDS")
        old_enabled = os.environ.get("TPLUS_DB_SYNC_REQUESTS_ENABLED")
        os.environ["TPLUS_SYNC_POLL_SECONDS"] = "30"
        os.environ["TPLUS_DB_SYNC_REQUESTS_ENABLED"] = "true"
        try:
            run_forever(
                sync_once=lambda: calls.append("full") or 0,
                sync_bom_once=lambda: calls.append("bom") or 0,
                fetch_db_full_request=lambda limit=5: requests.pop(0) if requests else None,
                finish_db_full_request=lambda request_id, status, code, detail: calls.append(
                    ("finish-full", request_id, status, code)
                ),
                read_sync_config=lambda: scheduled,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=lambda: scheduled,
                shadow_recorder=lambda _payload: [42],
                shadow_finisher=lambda *_args: None,
                now=lambda: current,
                sleep=lambda _seconds: None,
                max_runs=2,
            )
        finally:
            for key, value in {
                "TPLUS_SYNC_POLL_SECONDS": old_poll,
                "TPLUS_DB_SYNC_REQUESTS_ENABLED": old_enabled,
            }.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual("full", calls[0])
        self.assertEqual("full", calls[1])
        self.assertEqual(("finish-full", 9, "success", 0), calls[2])
        self.assertEqual("full", calls[3])


if __name__ == "__main__":
    unittest.main()
