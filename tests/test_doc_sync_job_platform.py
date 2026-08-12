from __future__ import annotations

import sys
import unittest
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"
sys.path.insert(0, str(WORKER_ROOT))

from app.storage.sync_job_platform import (  # noqa: E402
    SyncJobPlatformWriter,
    classify_error,
    platform_writer_for,
    safe_error_message,
)


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self.conn.sql.append(sql)

    def fetchone(self):
        return (31,)


class FakeConn:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.commits = 0
        self.rollback_count = 0
        self.closed = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.closed += 1


class FailingConn(FakeConn):
    def cursor(self) -> FakeCursor:
        raise RuntimeError("database unavailable")


class SyncJobPlatformWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = FakeConn()

    def test_job_upsert_refreshes_updated_at_without_overwriting_operator_fields(self):
        writer = SyncJobPlatformWriter(self.conn)
        writer.start_run(
            job_key="wecom.doc.17", kind="pull", provider="wecom",
            display_name="点检表", source_id=17, trigger="manual",
            legacy_ref={"table": "sync_runs", "id": 91},
        )
        sql = "\n".join(self.conn.sql)
        self.assertIn("updated_at = NOW()", sql)
        for protected in ("schedule =", "freshness_sla_seconds =", "artifact_glob =",
                          "alert_enabled =", "alert_chat_id ="):
            self.assertNotIn(protected, sql)

    def test_step_uses_the_unique_run_seq_conflict_target(self):
        SyncJobPlatformWriter(self.conn).upsert_step(31, 2, "fetch_page", "success", items=40)
        self.assertIn("ON CONFLICT (run_id, seq)", "\n".join(self.conn.sql))

    def test_platform_write_failure_rolls_back_and_returns_none(self):
        conn = FailingConn()
        result = SyncJobPlatformWriter(conn).start_run(
            job_key="wecom.doc.17", kind="pull", provider="wecom",
            display_name="点检表", source_id=17, trigger="manual", legacy_ref={},
        )
        self.assertIsNone(result)
        self.assertEqual(1, conn.rollback_count)

    def test_error_classifier_and_redaction(self):
        self.assertEqual("auth", classify_error(RuntimeError("access_token expired")))
        self.assertEqual("rate_limit", classify_error(RuntimeError("HTTP 429 too many requests")))
        self.assertEqual("network", classify_error(TimeoutError("read timed out")))
        safe = safe_error_message(RuntimeError("Authorization: Bearer secret-value"))
        self.assertNotIn("secret-value", safe)
        self.assertLessEqual(len(safe), 500)

    def test_close_only_closes_an_owned_connection(self):
        unowned = FakeConn()
        owned = FakeConn()
        SyncJobPlatformWriter(unowned).close()
        SyncJobPlatformWriter(owned, owns_connection=True).close()
        self.assertEqual(0, unowned.closed)
        self.assertEqual(1, owned.closed)

    def test_platform_writer_for_uses_noop_for_legacy_fake_store(self):
        writer = platform_writer_for(object())
        self.assertIsNone(writer.start_run(
            job_key="x", kind="pull", provider="wecom", display_name="x", source_id=1,
            trigger="manual", legacy_ref={},
        ))
