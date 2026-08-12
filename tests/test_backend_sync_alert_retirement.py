from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "services" / "backend-api" / "app" / "routers" / "ops.py"
NOTIFIER = ROOT / "services" / "doc-sync-worker" / "app" / "pipelines" / "sync_alert_notifier.py"
WORKER_LOOP = ROOT / "services" / "doc-sync-worker" / "app" / "pipelines" / "worker_loop.py"


class BackendSyncAlertRetirementTests(unittest.TestCase):
    def test_backend_no_longer_owns_sync_alert_threads(self) -> None:
        source = OPS.read_text(encoding="utf-8")
        for forbidden in (
            "_chanjet_token_alert_loop", "chanjet-token-watcher",
            "_tplus_full_sync_alert_loop", "tplus-full-sync-watcher",
            "CHANJET_ALERT_FEISHU_RECEIVE_ID",
        ):
            self.assertNotIn(forbidden, source)

    def test_unified_notifier_is_wired_before_legacy_tests_are_retired(self) -> None:
        notifier = NOTIFIER.read_text(encoding="utf-8")
        loop = WORKER_LOOP.read_text(encoding="utf-8")
        self.assertIn("credential_expiring", notifier)
        self.assertIn("run_notifier_once", loop)


if __name__ == "__main__":
    unittest.main()
