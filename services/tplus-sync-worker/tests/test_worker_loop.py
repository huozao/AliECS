import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
