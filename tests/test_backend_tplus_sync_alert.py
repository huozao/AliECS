"""T+ 定时全量失败告警：worker 侧原来只写日志和 integration_sync_runs，
失败后直接睡到下一个锚点（约 24h），没有任何人被通知。

2026-08-07 18:00 生产实测：全量在第一步 BOM 就失败，无告警，直到 /formula/colors/
显示 41 个「编码失联」才被发现。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class TPlusFullSyncAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        self._old_secret = os.environ.get("AUTH_TOKEN_SECRET")
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]
        sys.path.insert(0, backend_root)
        os.environ["AUTH_TOKEN_SECRET"] = "test-tplus-sync-alert-secret"
        from app.routers import ops

        self.module = ops
        self.now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        sys.path[:] = self._old_sys_path
        if self._old_secret is None:
            os.environ.pop("AUTH_TOKEN_SECRET", None)
        else:
            os.environ["AUTH_TOKEN_SECRET"] = self._old_secret
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]

    def test_latest_failed_run_is_not_ok(self) -> None:
        status = self.module.evaluate_tplus_full_sync(
            {"status": "failed", "finished_at": self.now - timedelta(hours=2), "exit_code": 3,
             "detail_json": {"failed_modules": ["bom"]}},
            now=self.now,
        )
        self.assertFalse(status["ok"])
        self.assertEqual(["bom"], status["failed_modules"])

    def test_partial_success_with_failed_modules_is_not_ok(self) -> None:
        """模块独立容错后，整轮可能 status=success 但个别模块挂了——同样要报。"""
        status = self.module.evaluate_tplus_full_sync(
            {"status": "success", "finished_at": self.now - timedelta(hours=1), "exit_code": 0,
             "detail_json": {"failed_modules": ["inventory"]}},
            now=self.now,
        )
        self.assertFalse(status["ok"])
        self.assertEqual(["inventory"], status["failed_modules"])

    def test_recent_clean_success_is_ok(self) -> None:
        status = self.module.evaluate_tplus_full_sync(
            {"status": "success", "finished_at": self.now - timedelta(hours=10), "exit_code": 0, "detail_json": {}},
            now=self.now,
        )
        self.assertTrue(status["ok"])

    def test_stale_success_is_not_ok(self) -> None:
        """全量是每日一轮；超过两轮没有成功记录说明 worker 自己也不对劲。"""
        status = self.module.evaluate_tplus_full_sync(
            {"status": "success", "finished_at": self.now - timedelta(hours=60), "exit_code": 0, "detail_json": {}},
            now=self.now,
        )
        self.assertFalse(status["ok"])
        self.assertTrue(status.get("stale"))

    def test_no_run_at_all_is_not_ok(self) -> None:
        status = self.module.evaluate_tplus_full_sync(None, now=self.now)
        self.assertFalse(status["ok"])

    def test_alert_text_names_the_failed_modules_and_points_at_the_runbook(self) -> None:
        text = self.module._tplus_full_sync_alert_text(
            {"ok": False, "status": "failed", "exit_code": 3, "failed_modules": ["bom", "inventory"],
             "finished_at": "2026-08-07T18:01:20+00:00", "stale": False}
        )
        self.assertIn("bom", text)
        self.assertIn("inventory", text)
        self.assertIn("runbooks/tplus.md", text)

    def test_alert_reuses_the_existing_ops_feishu_credentials(self) -> None:
        """不新增飞书凭据：与 openToken 告警走同一组 OPS_ALERT_FEISHU_* 配置。"""
        import inspect

        source = inspect.getsource(self.module._send_tplus_full_sync_alert)
        self.assertIn("OPS_ALERT_FEISHU_RECEIVE_ID", source)
        self.assertIn("OPS_ALERT_FEISHU_APP_ID", source)


if __name__ == "__main__":
    unittest.main()
