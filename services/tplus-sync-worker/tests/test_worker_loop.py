import os
import tempfile
import unittest
from pathlib import Path

from tplus_datahub.jobs.worker_loop import run_forever


class WorkerLoopTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
