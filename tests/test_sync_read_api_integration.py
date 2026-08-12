from __future__ import annotations

import importlib
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = (ROOT / "services" / "backend-api").resolve()
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _load_sync_read() -> Any:
    """Load backend sync reads without leaking an ambiguous top-level app."""
    old_path = list(sys.path)
    old_app_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    try:
        for name in tuple(old_app_modules):
            sys.modules.pop(name, None)
        sys.path.insert(0, str(BACKEND_ROOT))
        return importlib.import_module("app.sync_read")
    finally:
        sys.path[:] = old_path
        for name in tuple(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)
        sys.modules.update(old_app_modules)


sync_read = _load_sync_read()


class SyncReadPostgresIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.getenv("SYNC_JOB_PLATFORM_INTEGRATION") != "1":
            self.skipTest("set SYNC_JOB_PLATFORM_INTEGRATION=1 to run PostgreSQL integration")
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            self.skipTest("PostgreSQL integration requires DATABASE_URL")

        self.conn = psycopg.connect(database_url, connect_timeout=5)
        self.fixture_token = uuid.uuid4().hex
        self.job_key = ""
        self.source_id: int | None = None
        self.job_id: int | None = None
        self.success_run_id: int | None = None
        self.failed_run_id: int | None = None

    def _insert_fixture(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO external_sources(
                    provider, env_profile, source_name, source_type,
                    external_doc_id, external_sheet_id
                )
                VALUES ('wecom', 'ci', %s, 'smartsheet', %s, %s)
                RETURNING id
                """,
                (
                    f"P2 read integration {self.fixture_token}",
                    f"ci-doc-{self.fixture_token}",
                    f"ci-sheet-{self.fixture_token}",
                ),
            )
            self.source_id = int(cur.fetchone()[0])
            self.job_key = f"wecom.doc.{self.source_id}"
            cur.execute(
                """
                INSERT INTO sync_jobs(
                    job_key, kind, provider, display_name, schedule,
                    freshness_sla_seconds, alert_enabled, source_id
                )
                VALUES (%s, 'pull', 'wecom', 'P2 read integration', %s, 3600, TRUE, %s)
                RETURNING id
                """,
                (self.job_key, Jsonb({"kind": "integration"}), self.source_id),
            )
            self.job_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO sync_job_runs(
                    job_id, trigger, status, started_at, finished_at,
                    row_count, changed_count, detail_json, legacy_ref
                )
                VALUES (%s, 'manual', 'success', %s, %s, 12, 4, %s, %s)
                RETURNING id
                """,
                (
                    self.job_id,
                    NOW - timedelta(minutes=15),
                    NOW - timedelta(minutes=10),
                    Jsonb({"fixture": "p2-success"}),
                    Jsonb({}),
                ),
            )
            self.success_run_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO sync_job_runs(
                    job_id, trigger, status, started_at, finished_at,
                    row_count, changed_count, error_kind, error_message,
                    detail_json, legacy_ref
                )
                VALUES (%s, 'schedule', 'failed', %s, %s, 7, 0,
                        'network', 'CI synthetic failure', %s, %s)
                RETURNING id
                """,
                (
                    self.job_id,
                    NOW - timedelta(minutes=2),
                    NOW - timedelta(minutes=1),
                    Jsonb({"fixture": "p2-failed"}),
                    Jsonb({}),
                ),
            )
            self.failed_run_id = int(cur.fetchone()[0])
            cur.executemany(
                """
                INSERT INTO sync_job_steps(
                    run_id, seq, name, status, started_at, finished_at, items, message
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        self.failed_run_id,
                        3,
                        "persist",
                        "failed",
                        NOW - timedelta(seconds=80),
                        NOW - timedelta(seconds=60),
                        0,
                        "CI synthetic failure",
                    ),
                    (
                        self.failed_run_id,
                        1,
                        "fetch",
                        "success",
                        NOW - timedelta(seconds=120),
                        NOW - timedelta(seconds=100),
                        7,
                        None,
                    ),
                    (
                        self.failed_run_id,
                        2,
                        "transform",
                        "success",
                        NOW - timedelta(seconds=100),
                        NOW - timedelta(seconds=80),
                        7,
                        None,
                    ),
                ],
            )
            cur.execute(
                """
                INSERT INTO sync_job_alerts(
                    job_id, run_id, alert_kind, state, payload_json
                )
                VALUES (%s, %s, 'run_failed', 'open', %s)
                """,
                (self.job_id, self.failed_run_id, Jsonb({"fixture": "p2-alert"})),
            )
        self.conn.commit()

    def _cleanup(self) -> None:
        self.conn.rollback()
        if self.job_id is not None:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM sync_jobs WHERE id = %s", (self.job_id,))
            self.conn.commit()
            with self.conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM sync_jobs WHERE id = %s", (self.job_id,))
                remaining = int(cur.fetchone()[0])
            if remaining != 0:
                raise AssertionError(f"P2 integration fixture cleanup left job id {self.job_id}")
        if self.source_id is not None:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM external_sources WHERE id = %s", (self.source_id,))
            self.conn.commit()
        self.conn.close()

    def test_reads_overview_timeline_detail_and_alerts(self) -> None:
        try:
            self._insert_fixture()

            overview = sync_read.overview(self.conn, now=NOW)
            item = next(row for row in overview["items"] if row["job_key"] == self.job_key)
            self.assertEqual("failed", item["last_run"]["status"])
            self.assertEqual(self.source_id, item["source_id"])
            self.assertEqual("fresh", item["freshness"]["state"])
            self.assertEqual(1, item["open_alert_count"])

            page = sync_read.runs_page(
                self.conn,
                job_key=self.job_key,
                provider=None,
                status=None,
                limit=20,
                offset=0,
                now=NOW,
            )
            self.assertEqual(2, page["total"])
            self.assertEqual(
                [self.failed_run_id, self.success_run_id],
                [row["id"] for row in page["items"]],
            )

            detail = sync_read.run_detail(self.conn, self.failed_run_id, now=NOW)
            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual([1, 2, 3], [step["seq"] for step in detail["steps"]])
            self.assertEqual("网络异常", detail["run"]["error_label"])

            alerts = sync_read.alerts_page(
                self.conn,
                state="open",
                limit=50,
                offset=0,
            )
            matching = [row for row in alerts["items"] if row["job_key"] == self.job_key]
            self.assertEqual(1, len(matching))
            self.assertEqual(self.failed_run_id, matching[0]["run_id"])
        finally:
            self._cleanup()


if __name__ == "__main__":
    unittest.main()
