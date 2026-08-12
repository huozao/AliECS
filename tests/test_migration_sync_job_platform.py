from __future__ import annotations

import re
import unittest
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "migrations"
    / "0048_sync_job_platform.sql"
)


class SyncJobPlatformMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = MIGRATION.read_text(encoding="utf-8")

    def test_creates_four_tables(self) -> None:
        for table in ("sync_jobs", "sync_job_runs", "sync_job_steps", "sync_job_alerts"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", self.sql)

    def test_is_rerunnable(self) -> None:
        # 迁移按文件名排序全量扫过，必须可重复执行。
        # 用正则同时覆盖 CREATE TABLE / CREATE INDEX / CREATE UNIQUE INDEX：
        # 字符串子串断言 "CREATE INDEX idx_sync_job" 匹配不到
        # "CREATE UNIQUE INDEX idx_sync_job_alerts_open"（中间的 UNIQUE 打断了子串），会漏检。
        self.assertNotRegex(
            self.sql,
            r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(?!IF NOT EXISTS)",
        )

    def test_job_key_is_unique(self) -> None:
        self.assertIn("job_key TEXT NOT NULL UNIQUE", self.sql)

    def test_open_alert_is_deduped_by_partial_unique_index(self) -> None:
        # P3 notifier 的抢占去重完全依赖这条索引：一个作业一种告警同时只能有一条 open。
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_job_alerts_open", self.sql)
        self.assertIn("WHERE state = 'open'", self.sql)

    def test_runs_carry_error_kind_and_legacy_ref(self) -> None:
        # error_kind 是页面与告警的分类依据；legacy_ref 是双写期回指旧表的追溯键。
        self.assertIn("error_kind TEXT", self.sql)
        self.assertIn("legacy_ref JSONB", self.sql)

    def test_jobs_carry_freshness_and_artifact_fields(self) -> None:
        # 新鲜度与产出物新鲜度是本项目相对旧页面的核心增量，不能漏建。
        self.assertIn("freshness_sla_seconds INTEGER", self.sql)
        self.assertIn("artifact_glob TEXT", self.sql)

    def test_steps_reference_runs_with_cascade(self) -> None:
        self.assertIn("REFERENCES sync_job_runs(id) ON DELETE CASCADE", self.sql)

    def test_alert_state_vocabulary_is_pinned_by_check(self) -> None:
        # 去重偏索引只认 'open'。写成 'OPEN' 不报错、只静默绕过去重导致刷屏，
        # 所以词表必须由 CHECK 焊死而不是靠调用方自觉。
        self.assertIn("CHECK (state IN ('open', 'resolved'))", self.sql)

    def test_alerts_point_back_to_the_triggering_run(self) -> None:
        # 没有 run_id 的话，P2「点告警跳到那次运行」只能去解 payload_json，不可 join。
        # 可空是有意的：新鲜度类告警本就没有对应的 run。
        self.assertIn("run_id BIGINT REFERENCES sync_job_runs(id) ON DELETE SET NULL", self.sql)

    def test_steps_are_uniquely_keyed_by_run_and_seq(self) -> None:
        # P1 要按 (run_id, seq) upsert 步骤状态；不唯一就没有冲突键，并发下会插出重复步骤。
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_job_steps_run_seq", self.sql)


if __name__ == "__main__":
    unittest.main()
