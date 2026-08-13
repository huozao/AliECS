from __future__ import annotations

import importlib
import os
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from typing import Any

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - default local test run skips before use.
    psycopg = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
WORKER_ROOT = ROOT / "services" / "doc-sync-worker"


def _load_notifier() -> Any:
    old_path = list(sys.path)
    try:
        sys.path.insert(0, str(WORKER_ROOT))
        return importlib.import_module("app.pipelines.sync_alert_notifier")
    finally:
        sys.path[:] = old_path


class SyncAlertNotifierPostgresIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        database_url = os.getenv("SYNC_ALERT_INTEGRATION_DATABASE_URL", "").strip()
        if not database_url:
            self.skipTest("set SYNC_ALERT_INTEGRATION_DATABASE_URL to run PostgreSQL integration")
        if psycopg is None:
            self.fail("PostgreSQL integration requires psycopg")

        self.conn = psycopg.connect(database_url, connect_timeout=5)
        self.second_conn = psycopg.connect(database_url, connect_timeout=5)
        self.notifier = _load_notifier()
        self.job_key = f"ci.p3.{uuid.uuid4().hex}"
        self.job_id: int | None = None
        self.chanjet_before: tuple[Any, Any] | None = None
        self.chanjet_created = False

    def _insert_run(self, *, status: str, age_days: int = 0) -> int:
        assert self.job_id is not None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_job_runs(job_id, status, started_at, finished_at)
                VALUES (
                    %s, %s,
                    NOW() - (%s * INTERVAL '1 day'),
                    NOW() - (%s * INTERVAL '1 day')
                )
                RETURNING id
                """,
                (self.job_id, status, age_days, age_days),
            )
            run_id = int(cur.fetchone()[0])
        self.conn.commit()
        return run_id

    def _insert_step(self, run_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_job_steps(run_id, seq, name, status) VALUES (%s, 1, 'ci', 'success')",
                (run_id,),
            )
        self.conn.commit()

    def _open_alert_count(self, alert_kind: str) -> int:
        assert self.job_id is not None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM sync_job_alerts
                WHERE job_id = %s AND alert_kind = %s AND state = 'open'
                """,
                (self.job_id, alert_kind),
            )
            return int(cur.fetchone()[0])

    def _alert_notify_count_and_state(self, alert_id: int) -> tuple[int, str]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT notify_count, state FROM sync_job_alerts WHERE id = %s", (alert_id,))
            row = cur.fetchone()
        self.conn.commit()
        return int(row[0]), str(row[1])

    def _alert_state(self, alert_id: int) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT state FROM sync_job_alerts WHERE id = %s", (alert_id,))
            row = cur.fetchone()
        self.conn.commit()
        return str(row[0])

    def _exercise_chanjet_coalesce(self, repo: Any) -> None:
        custom_sla = 98765
        custom_glob = "/operator/kept/*.xlsx"
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT freshness_sla_seconds, artifact_glob FROM sync_jobs WHERE job_key = 'chanjet.full'"
            )
            self.chanjet_before = cur.fetchone()
            if self.chanjet_before is None:
                cur.execute(
                    """
                    INSERT INTO sync_jobs(job_key, kind, provider, display_name, freshness_sla_seconds, artifact_glob)
                    VALUES ('chanjet.full', 'pull', 'chanjet', 'CI chanjet', %s, %s)
                    """,
                    (custom_sla, custom_glob),
                )
                self.chanjet_created = True
            else:
                cur.execute(
                    """
                    UPDATE sync_jobs
                    SET freshness_sla_seconds = %s, artifact_glob = %s
                    WHERE job_key = 'chanjet.full'
                    """,
                    (custom_sla, custom_glob),
                )
        self.conn.commit()

        repo.ensure_chanjet_defaults()
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT freshness_sla_seconds, artifact_glob FROM sync_jobs WHERE job_key = 'chanjet.full'"
            )
            self.assertEqual((custom_sla, custom_glob), cur.fetchone())
        self.conn.commit()

    def test_real_postgres_claim_delivery_resolution_reopen_and_retention(self) -> None:
        now = datetime.now(timezone.utc)
        repo = self.notifier.SyncAlertRepository(self.conn, now_fn=lambda: now)
        second_repo = self.notifier.SyncAlertRepository(self.second_conn, now_fn=lambda: now)
        delivered_texts: list[str] = []

        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sync_jobs(job_key, kind, provider, display_name)
                    VALUES (%s, 'pull', 'ci', 'P3 notifier integration')
                    RETURNING id
                    """,
                    (self.job_key,),
                )
                self.job_id = int(cur.fetchone()[0])
            self.conn.commit()
            job = {"id": self.job_id, "job_key": self.job_key, "display_name": "P3 notifier integration"}
            first_run_id = self._insert_run(status="failed")

            barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as executor:
                first, second = list(executor.map(
                    lambda item: (barrier.wait(), item[0].claim_alert(job, first_run_id, "failed", {}))[1],
                    ((repo,), (second_repo,)),
                ))
            self.assertEqual([False, True], sorted([bool(first), bool(second)]))
            self.assertEqual(1, self._open_alert_count("failed"))
            alert_id = int(first or second)

            def fake_sender(alert: dict[str, Any]) -> bool:
                delivered_texts.append(self.notifier.build_alert_text("open", alert, now=now))
                return True

            self.assertTrue(repo.deliver_due(alert_id, fake_sender))
            self.assertEqual((1, "open"), self._alert_notify_count_and_state(alert_id))
            recovery_payload = {"status": "success", "job_key": self.job_key}
            self.assertTrue(repo.resolve_alert(
                alert_id,
                recovery_payload,
                lambda alert: delivered_texts.append(
                    self.notifier.build_alert_text("resolved", alert, now=now)
                ) is None,
            ))
            self.assertEqual("resolved", self._alert_state(alert_id))
            self.assertEqual(2, len(delivered_texts))
            self.assertTrue(all(isinstance(text, str) and text for text in delivered_texts))

            newer_run_id = self._insert_run(status="failed")
            self.assertIsNotNone(repo.claim_alert(job, newer_run_id, "failed", {}))

            old_success_id = self._insert_run(status="success", age_days=31)
            old_failed_id = self._insert_run(status="failed", age_days=91)
            self._insert_step(old_success_id)
            self._insert_step(old_failed_id)
            self.assertEqual(2, repo.cleanup_steps())
            with self.conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM sync_job_steps WHERE run_id IN (%s, %s)", (old_success_id, old_failed_id))
                self.assertEqual(0, int(cur.fetchone()[0]))
            self.conn.commit()

            self._exercise_chanjet_coalesce(repo)
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        try:
            self.conn.rollback()
            self.second_conn.rollback()
            with self.conn.cursor() as cur:
                if self.chanjet_created:
                    cur.execute("DELETE FROM sync_jobs WHERE job_key = 'chanjet.full'")
                elif self.chanjet_before is not None:
                    cur.execute(
                        """
                        UPDATE sync_jobs SET freshness_sla_seconds = %s, artifact_glob = %s
                        WHERE job_key = 'chanjet.full'
                        """,
                        self.chanjet_before,
                    )
                if self.job_id is not None:
                    cur.execute("DELETE FROM sync_jobs WHERE id = %s", (self.job_id,))
                    cur.execute(
                        """
                        SELECT
                          (SELECT count(*) FROM sync_jobs WHERE id = %s),
                          (SELECT count(*) FROM sync_job_runs WHERE job_id = %s),
                          (SELECT count(*) FROM sync_job_steps s JOIN sync_job_runs r ON r.id = s.run_id WHERE r.job_id = %s),
                          (SELECT count(*) FROM sync_job_alerts WHERE job_id = %s)
                        """,
                        (self.job_id, self.job_id, self.job_id, self.job_id),
                    )
                    self.assertEqual((0, 0, 0, 0), tuple(int(value) for value in cur.fetchone()))
            self.conn.commit()
        finally:
            self.second_conn.close()
            self.conn.close()

    def tearDown(self) -> None:
        if not self.conn.closed:
            self._cleanup()


if __name__ == "__main__":
    unittest.main()
