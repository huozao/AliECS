from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[1]
DOC_WORKER_ROOT = (ROOT / "services" / "doc-sync-worker").resolve()
DOC_WRITER_PATH = DOC_WORKER_ROOT / "app" / "storage" / "sync_job_platform.py"
TPLUS_WORKER_ROOT = (ROOT / "services" / "tplus-sync-worker" / "src").resolve()


def _load_writers() -> tuple[Any, Any]:
    """Load both real writers without leaving an ambiguous top-level app import."""
    old_path = list(sys.path)
    try:
        sys.path.insert(0, str(DOC_WORKER_ROOT))
        sys.path.insert(0, str(TPLUS_WORKER_ROOT))

        spec = importlib.util.spec_from_file_location(
            "_sync_job_platform_doc_integration", DOC_WRITER_PATH
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load doc sync writer from {DOC_WRITER_PATH}")
        doc_writer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doc_writer)

        tplus_writer = importlib.import_module("tplus_datahub.jobs.sync_job_platform")
        return doc_writer, tplus_writer
    finally:
        sys.path[:] = old_path


class SyncJobPlatformPostgresIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.getenv("SYNC_JOB_PLATFORM_INTEGRATION") != "1":
            self.skipTest("set SYNC_JOB_PLATFORM_INTEGRATION=1 to run PostgreSQL integration")
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            self.skipTest("PostgreSQL integration requires DATABASE_URL")

        self.conn = psycopg.connect(database_url, connect_timeout=5)
        self.doc_writer_module, self.tplus_writer = _load_writers()
        self.source_id: int | None = None
        self.doc_job_id: int | None = None
        self.doc_run_id: int | None = None
        self.doc_legacy_run_id: int | None = None
        self.tplus_job_id: int | None = None
        self.tplus_run_id: int | None = None
        self.tplus_legacy_run_id: int | None = None
        self.tplus_job_before: tuple[Any, ...] | None = None

    def _cleanup(self) -> None:
        try:
            self.conn.rollback()
            with self.conn.cursor() as cur:
                for run_id in (self.doc_run_id, self.tplus_run_id):
                    if run_id is not None:
                        cur.execute("DELETE FROM sync_job_runs WHERE id = %s", (run_id,))

                if self.doc_job_id is not None:
                    cur.execute("DELETE FROM sync_jobs WHERE id = %s", (self.doc_job_id,))

                if self.tplus_job_id is not None:
                    if self.tplus_job_before is None:
                        cur.execute("DELETE FROM sync_jobs WHERE id = %s", (self.tplus_job_id,))
                    else:
                        cur.execute(
                            """
                            UPDATE sync_jobs
                            SET kind = %s, provider = %s, display_name = %s,
                                source_id = %s, updated_at = %s
                            WHERE id = %s
                            """,
                            (*self.tplus_job_before[1:], self.tplus_job_before[0]),
                        )

                if self.source_id is not None:
                    cur.execute("DELETE FROM external_sources WHERE id = %s", (self.source_id,))
                if self.doc_legacy_run_id is not None:
                    cur.execute("DELETE FROM sync_runs WHERE id = %s", (self.doc_legacy_run_id,))
                if self.tplus_legacy_run_id is not None:
                    cur.execute(
                        "DELETE FROM integration_sync_runs WHERE id = %s",
                        (self.tplus_legacy_run_id,),
                    )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self.conn.close()

    def test_doc_and_tplus_writers_persist_real_runs_steps_and_legacy_refs(self) -> None:
        unique = uuid.uuid4().hex
        try:
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
                    (f"CI sync platform {unique}", f"ci-doc-{unique}", f"ci-sheet-{unique}"),
                )
                self.source_id = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO sync_runs(provider, env_profile, mode, status)
                    VALUES ('wecom', 'ci', 'integration', 'success')
                    RETURNING id
                    """
                )
                self.doc_legacy_run_id = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO integration_sync_runs(provider, module, mode, status)
                    VALUES ('chanjet', 'all', 'integration', 'failed')
                    RETURNING id
                    """
                )
                self.tplus_legacy_run_id = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT id, kind, provider, display_name, source_id, updated_at
                    FROM sync_jobs WHERE job_key = 'chanjet.full'
                    """
                )
                self.tplus_job_before = cur.fetchone()
            self.conn.commit()

            doc_writer = self.doc_writer_module.SyncJobPlatformWriter(self.conn)
            doc_job_key = f"wecom.doc.{self.source_id}"
            self.doc_run_id = doc_writer.start_run(
                job_key=doc_job_key,
                kind="pull",
                provider="wecom",
                display_name="CI document integration",
                source_id=self.source_id,
                trigger="manual",
                legacy_ref={"table": "sync_runs", "id": self.doc_legacy_run_id},
            )
            self.assertIsNotNone(self.doc_run_id)
            doc_writer.upsert_step(self.doc_run_id, 1, "fetch_records", "running")
            doc_writer.upsert_step(self.doc_run_id, 1, "fetch_records", "success", items=7)
            doc_writer.finish_run(
                self.doc_run_id,
                status="success",
                row_count=7,
                changed_count=3,
                error=None,
                detail_json={"source": "ci"},
            )

            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id, job_key, source_id, updated_at FROM sync_jobs WHERE job_key = %s",
                    (doc_job_key,),
                )
                doc_job = cur.fetchone()
                self.assertIsNotNone(doc_job)
                self.doc_job_id = int(doc_job[0])
                self.assertEqual((doc_job_key, self.source_id), doc_job[1:3])
                self.assertIsNotNone(doc_job[3])
                cur.execute(
                    """
                    SELECT trigger, status, row_count, changed_count, legacy_ref
                    FROM sync_job_runs WHERE id = %s
                    """,
                    (self.doc_run_id,),
                )
                self.assertEqual(
                    ("manual", "success", 7, 3, {"table": "sync_runs", "id": self.doc_legacy_run_id}),
                    cur.fetchone(),
                )
                cur.execute(
                    "SELECT seq, name, status, items FROM sync_job_steps WHERE run_id = %s ORDER BY seq",
                    (self.doc_run_id,),
                )
                self.assertEqual([(1, "fetch_records", "success", 7)], cur.fetchall())

            self.tplus_run_id = self.tplus_writer.start_run(
                job_key="chanjet.full",
                kind="pull",
                provider="chanjet",
                display_name="T+ full sync",
                source_id=None,
                trigger="schedule",
                legacy_ref={},
            )
            self.assertIsNotNone(self.tplus_run_id)
            self.tplus_writer.upsert_step(self.tplus_run_id, 1, "fetch_bom", "running")
            self.tplus_writer.upsert_step(
                self.tplus_run_id, 1, "fetch_bom", "failed", items=2, message="CI failure"
            )
            self.tplus_writer.finish_run(
                self.tplus_run_id,
                status="failed",
                row_count=2,
                changed_count=0,
                error=RuntimeError("database write failed"),
                detail_json={"module": "bom"},
            )
            self.tplus_writer.attach_legacy_ref(self.tplus_run_id, self.tplus_legacy_run_id)

            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id, job_key, source_id, updated_at FROM sync_jobs WHERE job_key = 'chanjet.full'"
                )
                tplus_job = cur.fetchone()
                self.assertIsNotNone(tplus_job)
                self.tplus_job_id = int(tplus_job[0])
                self.assertEqual(("chanjet.full", None), tplus_job[1:3])
                self.assertIsNotNone(tplus_job[3])
                cur.execute(
                    """
                    SELECT trigger, status, error_kind, legacy_ref
                    FROM sync_job_runs WHERE id = %s
                    """,
                    (self.tplus_run_id,),
                )
                self.assertEqual(
                    (
                        "schedule",
                        "failed",
                        "write",
                        {"table": "integration_sync_runs", "id": self.tplus_legacy_run_id},
                    ),
                    cur.fetchone(),
                )
                cur.execute(
                    "SELECT seq, name, status, items FROM sync_job_steps WHERE run_id = %s ORDER BY seq",
                    (self.tplus_run_id,),
                )
                self.assertEqual([(1, "fetch_bom", "failed", 2)], cur.fetchall())
        finally:
            self._cleanup()


if __name__ == "__main__":
    unittest.main()
