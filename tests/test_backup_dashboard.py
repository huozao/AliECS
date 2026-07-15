from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"
BACKUPS_PAGE = ROOT / "services" / "public-web" / "backups" / "index.html"
HEALTH_PAGE = ROOT / "services" / "public-web" / "health" / "index.html"


class BackupDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [item for item in sys.path if item != str(BACKEND_ROOT)]

    def test_migration_seeds_full_backup_inventory(self) -> None:
        sql = (ROOT / "db" / "migrations" / "0030_backup_observability.sql").read_text(encoding="utf-8")
        for code in [
            "core-restic", "quality-reports", "tplus-raw", "wecom-structure",
            "source-config", "webdock-browser-data", "webdock-system-image",
        ]:
            self.assertIn(f"'{code}'", sql)
        self.assertIn("polymerwang", sql)
        self.assertIn("polymerone", sql)

    def test_health_has_backup_summary_and_entry(self) -> None:
        html = HEALTH_PAGE.read_text(encoding="utf-8")
        self.assertIn("备份与恢复", html)
        self.assertIn('href="/backups/"', html)
        self.assertIn("function renderBackupSummary(", html)

    def test_catalog_correction_uses_single_core_repository_and_planned_quality_pool(self) -> None:
        sql = (ROOT / "db" / "migrations" / "0031_backup_catalog_correction.sql").read_text(encoding="utf-8")
        self.assertIn("核心系统 Restic（单仓库）", sql)
        self.assertIn("核心 Restic（polymerone）", sql)
        self.assertIn("不保存重复副本", sql)
        self.assertIn("质检报告文件存储（待建设）", sql)
        self.assertIn("尚未实施，不代表文件已经备份", sql)

    def test_backup_page_has_catalog_and_history_api(self) -> None:
        html = BACKUPS_PAGE.read_text(encoding="utf-8")
        self.assertIn("备份总账", html)
        self.assertIn("/v1/ops/backups", html)
        self.assertIn("运行历史", html)
        self.assertIn("明确排除", html)
        self.assertIn("恢复校验", html)
        self.assertIn("未执行", html)

    def test_policy_classification_uses_freshness_and_replica_status(self) -> None:
        from app.routers.backups import _classify_policy

        now = datetime.now(timezone.utc)
        base = {
            "lifecycle_status": "active",
            "warning_after_seconds": 30 * 3600,
            "failure_after_seconds": 48 * 3600,
            "latest_run": {
                "status": "success",
                "finished_at_raw": now - timedelta(hours=3),
                "destinations": [{"name": "primary", "status": "ok"}, {"name": "secondary", "status": "ok"}],
            },
        }
        self.assertEqual("ok", _classify_policy(base, now))
        stale = {**base, "latest_run": {**base["latest_run"], "finished_at_raw": now - timedelta(hours=31)}}
        self.assertEqual("warning", _classify_policy(stale, now))
        broken_replica = {**base, "latest_run": {**base["latest_run"], "destinations": [{"status": "ok"}, {"status": "failed"}]}}
        self.assertEqual("warning", _classify_policy(broken_replica, now))

    def test_internal_report_route_is_registered(self) -> None:
        from app.main import app

        paths = {route.path for route in app.routes}
        self.assertIn("/v1/internal/backups/report", paths)
        self.assertIn("/v1/ops/backups", paths)


if __name__ == "__main__":
    unittest.main()
