from __future__ import annotations

import importlib
import importlib.util
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
DOC_WORKER_ROOT = (ROOT / "services" / "doc-sync-worker").resolve()
DOC_STORE_PATH = DOC_WORKER_ROOT / "app" / "storage" / "postgres.py"
TPLUS_WORKER_ROOT = (ROOT / "services" / "tplus-sync-worker" / "src").resolve()

CONFIG = {
    "enabled": True,
    "interval_seconds": 86400,
    "anchor_time": "01:00",
}
SHADOW = {
    "mode": "shadow",
    "sampled_at": "2026-08-13T10:00:00+00:00",
    "legacy": {"run_full": False, "due": "2026-08-13T17:00:00+00:00"},
    "candidate": {"run_full": False, "due": "2026-08-13T17:00:00+00:00"},
    "decision_match": True,
    "due_delta_seconds": 0.0,
}


def _load_repositories() -> tuple[Any, Any]:
    """Load both real worker repositories without leaking an ambiguous app package."""
    old_path = list(sys.path)
    old_app_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    try:
        for name in tuple(old_app_modules):
            sys.modules.pop(name, None)
        sys.path.insert(0, str(DOC_WORKER_ROOT))
        spec = importlib.util.spec_from_file_location(
            "_p4_scheduler_doc_repository", DOC_STORE_PATH
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load doc repository from {DOC_STORE_PATH}")
        doc_repository = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doc_repository)

        sys.path.insert(0, str(TPLUS_WORKER_ROOT))
        tplus_repository = importlib.import_module(
            "tplus_datahub.jobs.db_sync_requests"
        )
        return doc_repository, tplus_repository
    finally:
        sys.path[:] = old_path
        for name in tuple(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)
        sys.modules.update(old_app_modules)


class SyncSchedulerPostgresIntegrationTests(unittest.TestCase):
    def test_real_shadow_writers_update_only_existing_scheduled_runs(self) -> None:
        database_url = os.getenv("SYNC_SCHEDULER_INTEGRATION_DATABASE_URL", "").strip()
        if not database_url:
            self.skipTest(
                "set SYNC_SCHEDULER_INTEGRATION_DATABASE_URL to run PostgreSQL integration"
            )

        doc_repository, tplus_repository = _load_repositories()
        conn = psycopg.connect(database_url, connect_timeout=5)
        fixture_key = f"ci.p4.{uuid.uuid4().hex}"
        ci_job_id: int | None = None
        chanjet_job_id: int | None = None
        chanjet_before: tuple[dict[str, Any], datetime] | None = None
        created_chanjet_job = False
        created_run_ids: list[int] = []

        try:
            base = datetime.now(timezone.utc)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sync_jobs(job_key, kind, provider, display_name, schedule)
                    VALUES (%s, 'pull', 'wecom', 'P4 scheduler integration', %s)
                    RETURNING id
                    """,
                    (fixture_key, Jsonb(CONFIG)),
                )
                ci_job_id = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO sync_job_runs(job_id, trigger, status, started_at, detail_json)
                    VALUES (%s, 'schedule', 'success', %s, %s),
                           (%s, 'schedule', 'success', %s, %s),
                           (%s, 'manual', 'success', %s, %s)
                    RETURNING id
                    """,
                    (
                        ci_job_id,
                        base,
                        Jsonb({"fixture": "older-schedule"}),
                        ci_job_id,
                        base + timedelta(seconds=1),
                        Jsonb({"fixture": "latest-schedule"}),
                        ci_job_id,
                        base + timedelta(seconds=2),
                        Jsonb({"fixture": "newer-manual"}),
                    ),
                )
                doc_run_ids = [int(row[0]) for row in cur.fetchall()]
                created_run_ids.extend(doc_run_ids)
                doc_run_id = doc_run_ids[1]
                cur.execute(
                    """
                    INSERT INTO sync_job_steps(run_id, seq, name, status)
                    VALUES (%s, 1, 'fixture', 'success')
                    """,
                    (doc_run_id,),
                )
                cur.execute(
                    """
                    INSERT INTO sync_job_alerts(
                        job_id, run_id, alert_kind, state, payload_json
                    )
                    VALUES (%s, %s, 'ci_fixture', 'open', %s)
                    """,
                    (ci_job_id, doc_run_id, Jsonb({"fixture": fixture_key})),
                )

                cur.execute(
                    """
                    SELECT id, schedule, updated_at
                    FROM sync_jobs
                    WHERE job_key = 'chanjet.full'
                    FOR UPDATE
                    """
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        INSERT INTO sync_jobs(
                            job_key, kind, provider, display_name, schedule
                        )
                        VALUES ('chanjet.full', 'pull', 'chanjet', 'T+ full sync', '{}')
                        RETURNING id
                        """
                    )
                    chanjet_job_id = int(cur.fetchone()[0])
                    created_chanjet_job = True
                else:
                    chanjet_job_id = int(row[0])
                    chanjet_before = (dict(row[1]), row[2])
                    cur.execute(
                        """
                        UPDATE sync_jobs
                        SET schedule = '{}'::jsonb, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (chanjet_job_id,),
                    )

                cur.execute(
                    """
                    SELECT COALESCE(MAX(started_at), NOW())
                    FROM sync_job_runs
                    WHERE job_id = %s
                    """,
                    (chanjet_job_id,),
                )
                latest_started_at = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO sync_job_runs(job_id, trigger, status, started_at, detail_json)
                    VALUES (%s, 'schedule', 'success', %s, %s),
                           (%s, 'manual', 'success', %s, %s)
                    RETURNING id
                    """,
                    (
                        chanjet_job_id,
                        latest_started_at + timedelta(seconds=1),
                        Jsonb({"fixture": fixture_key}),
                        chanjet_job_id,
                        latest_started_at + timedelta(seconds=2),
                        Jsonb({"fixture": f"{fixture_key}.manual"}),
                    ),
                )
                chanjet_run_ids = [int(row[0]) for row in cur.fetchall()]
                created_run_ids.extend(chanjet_run_ids)
                chanjet_run_id = chanjet_run_ids[0]
            conn.commit()

            doc_store = doc_repository.PostgresDocSyncStore(conn)
            before_doc_run_count = self._run_count(conn, ci_job_id)
            exact_doc_updated_ids = doc_store.record_scheduler_shadow(SHADOW)
            self.assertEqual([doc_run_id], exact_doc_updated_ids)
            doc_store.finish_scheduler_shadow(
                exact_doc_updated_ids,
                observed_sleep_seconds=123,
                candidate_would_wake=False,
            )
            self.assertEqual(before_doc_run_count, self._run_count(conn, ci_job_id))
            doc_details = self._details_by_id(conn, doc_run_ids)
            self.assertNotIn("shadow", doc_details[doc_run_ids[0]])
            self.assertNotIn("shadow", doc_details[doc_run_ids[2]])
            self.assertEqual("shadow", doc_details[doc_run_id]["shadow"]["mode"])
            self.assertEqual(
                123,
                doc_details[doc_run_id]["shadow"]["observed_sleep_seconds"],
            )

            tplus_repository.seed_platform_schedule(CONFIG, conn=conn)
            stored_schedule = tplus_repository.fetch_platform_schedule(conn=conn)
            self.assertEqual(CONFIG, stored_schedule)
            before_tplus_run_count = self._run_count(conn, chanjet_job_id)
            exact_updated_ids = tplus_repository.record_scheduler_shadow(
                SHADOW, conn=conn
            )
            self.assertEqual([chanjet_run_id], exact_updated_ids)
            tplus_repository.finish_scheduler_shadow(
                exact_updated_ids,
                observed_sleep_seconds=123,
                candidate_would_wake=False,
                conn=conn,
            )
            detail_json = self._details_by_id(conn, [chanjet_run_id])[chanjet_run_id]
            self.assertEqual("shadow", detail_json["shadow"]["mode"])
            self.assertEqual(123, detail_json["shadow"]["observed_sleep_seconds"])
            synthetic_shadow_run_count = (
                self._run_count(conn, chanjet_job_id) - before_tplus_run_count
            )
            self.assertEqual(0, synthetic_shadow_run_count)

            tplus_repository.finish_scheduler_shadow(
                exact_updated_ids,
                observed_sleep_seconds=2**40,
                candidate_would_wake=True,
                conn=conn,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                self.assertEqual(1, int(cur.fetchone()[0]))
            detail_after_failure = self._details_by_id(conn, [chanjet_run_id])[
                chanjet_run_id
            ]
            self.assertEqual(
                123,
                detail_after_failure["shadow"]["observed_sleep_seconds"],
            )
        finally:
            conn.rollback()
            try:
                with conn.cursor() as cur:
                    if created_run_ids:
                        cur.execute(
                            "DELETE FROM sync_job_runs WHERE id = ANY(%s)",
                            (created_run_ids,),
                        )
                    if ci_job_id is not None:
                        cur.execute("DELETE FROM sync_jobs WHERE id = %s", (ci_job_id,))
                    if chanjet_job_id is not None:
                        if created_chanjet_job:
                            cur.execute(
                                "DELETE FROM sync_jobs WHERE id = %s",
                                (chanjet_job_id,),
                            )
                        elif chanjet_before is not None:
                            cur.execute(
                                """
                                UPDATE sync_jobs
                                SET schedule = %s, updated_at = %s
                                WHERE id = %s
                                """,
                                (
                                    Jsonb(chanjet_before[0]),
                                    chanjet_before[1],
                                    chanjet_job_id,
                                ),
                            )
                conn.commit()
                residue = self._fixture_residue(
                    conn,
                    fixture_key,
                    created_run_ids,
                    ci_job_id,
                )
                self.assertEqual((0, 0, 0, 0), residue)
            finally:
                conn.close()

    @staticmethod
    def _run_count(conn: Any, job_id: int) -> int:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sync_job_runs WHERE job_id = %s", (job_id,))
            return int(cur.fetchone()[0])

    @staticmethod
    def _details_by_id(conn: Any, run_ids: list[int]) -> dict[int, dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, detail_json FROM sync_job_runs WHERE id = ANY(%s)",
                (run_ids,),
            )
            return {int(row[0]): dict(row[1]) for row in cur.fetchall()}

    @staticmethod
    def _fixture_residue(
        conn: Any,
        fixture_key: str,
        run_ids: list[int],
        ci_job_id: int | None,
    ) -> tuple[int, int, int, int]:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sync_jobs WHERE job_key = %s", (fixture_key,))
            job_count = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM sync_job_runs WHERE id = ANY(%s)", (run_ids,))
            run_count = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM sync_job_steps WHERE run_id = ANY(%s)", (run_ids,))
            step_count = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT COUNT(*)
                FROM sync_job_alerts
                WHERE run_id = ANY(%s) OR job_id = %s
                """,
                (run_ids, ci_job_id),
            )
            alert_count = int(cur.fetchone()[0])
        return job_count, run_count, step_count, alert_count


if __name__ == "__main__":
    unittest.main()
