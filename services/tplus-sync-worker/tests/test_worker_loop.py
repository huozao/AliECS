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
                    "detail_json": {"run": 1},
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
        self.assertEqual({"run": 1}, recorded[0]["detail_json"])

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


if __name__ == "__main__":
    unittest.main()
