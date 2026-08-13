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
from unittest import mock

import psycopg
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[1]
DOC_WORKER_ROOT = (ROOT / "services" / "doc-sync-worker").resolve()
DOC_STORE_PATH = DOC_WORKER_ROOT / "app" / "storage" / "postgres.py"
DOC_SCHEDULER_PATH = DOC_WORKER_ROOT / "app" / "pipelines" / "sync_scheduler.py"
TPLUS_WORKER_ROOT = (ROOT / "services" / "tplus-sync-worker" / "src").resolve()
DOC_REPOSITORY_MODULE = "_p4_scheduler_doc_repository"
DOC_SCHEDULER_MODULE = "_p4_scheduler_doc_kernel"
_MISSING = object()

CONFIG = {
    "enabled": True,
    "interval_seconds": 86400,
    "anchor_time": "01:00",
}


def _real_shadow_payload() -> dict[str, object]:
    module_name = DOC_SCHEDULER_MODULE
    previous_module = sys.modules.get(module_name, _MISSING)
    spec = importlib.util.spec_from_file_location(module_name, DOC_SCHEDULER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load scheduler from {DOC_SCHEDULER_PATH}")
    scheduler = importlib.util.module_from_spec(spec)
    try:
        sys.modules[module_name] = scheduler
        spec.loader.exec_module(scheduler)
        sampled_at = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        decision = scheduler.ScheduleDecision(
            datetime(2026, 8, 13, 17, 0, tzinfo=timezone.utc),
            False,
            25200,
        )
        return scheduler.shadow_payload(
            sampled_at=sampled_at,
            legacy=decision,
            candidate=decision,
        )
    finally:
        if previous_module is _MISSING:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module


def _load_repositories() -> tuple[Any, Any]:
    """Load both real worker repositories without leaking an ambiguous app package."""
    old_path = list(sys.path)
    old_app_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    previous_repository_module = sys.modules.get(DOC_REPOSITORY_MODULE, _MISSING)
    try:
        for name in tuple(old_app_modules):
            sys.modules.pop(name, None)
        sys.path.insert(0, str(DOC_WORKER_ROOT))
        spec = importlib.util.spec_from_file_location(
            DOC_REPOSITORY_MODULE, DOC_STORE_PATH
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load doc repository from {DOC_STORE_PATH}")
        doc_repository = importlib.util.module_from_spec(spec)
        sys.modules[DOC_REPOSITORY_MODULE] = doc_repository
        spec.loader.exec_module(doc_repository)

        sys.path.insert(0, str(TPLUS_WORKER_ROOT))
        tplus_repository = importlib.import_module(
            "tplus_datahub.jobs.db_sync_requests"
        )
        return doc_repository, tplus_repository
    finally:
        sys.path[:] = old_path
        if previous_repository_module is _MISSING:
            sys.modules.pop(DOC_REPOSITORY_MODULE, None)
        else:
            sys.modules[DOC_REPOSITORY_MODULE] = previous_repository_module
        for name in tuple(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)
        sys.modules.update(old_app_modules)


class SyncSchedulerIntegrationHelperTests(unittest.TestCase):
    def test_load_repositories_imports_dataclasses_and_restores_process_state(self) -> None:
        sentinel = object()
        previous = sys.modules.get(DOC_REPOSITORY_MODULE)
        had_previous = DOC_REPOSITORY_MODULE in sys.modules
        original_path = list(sys.path)
        sys.modules[DOC_REPOSITORY_MODULE] = sentinel
        try:
            doc_repository, tplus_repository = _load_repositories()
            self.assertTrue(hasattr(doc_repository, "PostgresDocSyncStore"))
            self.assertTrue(hasattr(tplus_repository, "record_scheduler_shadow"))
            self.assertIs(sentinel, sys.modules[DOC_REPOSITORY_MODULE])
            self.assertEqual(original_path, sys.path)
        finally:
            if had_previous:
                sys.modules[DOC_REPOSITORY_MODULE] = previous
            else:
                sys.modules.pop(DOC_REPOSITORY_MODULE, None)

    def test_real_shadow_payload_restores_existing_temporary_module(self) -> None:
        sentinel = object()
        previous = sys.modules.get(DOC_SCHEDULER_MODULE)
        had_previous = DOC_SCHEDULER_MODULE in sys.modules
        original_path = list(sys.path)
        sys.modules[DOC_SCHEDULER_MODULE] = sentinel
        try:
            payload = _real_shadow_payload()
            self.assertEqual("shadow", payload["mode"])
            self.assertIs(sentinel, sys.modules[DOC_SCHEDULER_MODULE])
            self.assertEqual(original_path, sys.path)
        finally:
            if had_previous:
                sys.modules[DOC_SCHEDULER_MODULE] = previous
            else:
                sys.modules.pop(DOC_SCHEDULER_MODULE, None)

    def test_nonempty_database_url_reaches_mocked_connection_after_real_imports(self) -> None:
        case = SyncSchedulerPostgresIntegrationTests(
            "test_real_shadow_writers_update_only_existing_scheduled_runs"
        )
        previous_modules = {
            name: sys.modules.pop(name, _MISSING)
            for name in (DOC_REPOSITORY_MODULE, DOC_SCHEDULER_MODULE)
        }
        try:
            with mock.patch.dict(
                os.environ,
                {"SYNC_SCHEDULER_INTEGRATION_DATABASE_URL": "postgresql://invalid.example/ci"},
            ), mock.patch.object(
                psycopg,
                "connect",
                side_effect=RuntimeError("mock connection boundary"),
            ) as connect:
                with self.assertRaisesRegex(RuntimeError, "mock connection boundary"):
                    case.test_real_shadow_writers_update_only_existing_scheduled_runs()
            connect.assert_called_once_with(
                "postgresql://invalid.example/ci",
                connect_timeout=5,
            )
            self.assertNotIn(DOC_REPOSITORY_MODULE, sys.modules)
            self.assertNotIn(DOC_SCHEDULER_MODULE, sys.modules)
        finally:
            for name, previous in previous_modules.items():
                if previous is not _MISSING:
                    sys.modules[name] = previous


class SyncSchedulerPostgresIntegrationTests(unittest.TestCase):
    def test_real_shadow_writers_update_only_existing_scheduled_runs(self) -> None:
        database_url = os.getenv("SYNC_SCHEDULER_INTEGRATION_DATABASE_URL", "").strip()
        if not database_url:
            self.skipTest(
                "set SYNC_SCHEDULER_INTEGRATION_DATABASE_URL to run PostgreSQL integration"
            )

        doc_repository, tplus_repository = _load_repositories()
        shadow = _real_shadow_payload()
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
            exact_doc_updated_ids = doc_store.record_scheduler_shadow(shadow)
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
                shadow, conn=conn
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
