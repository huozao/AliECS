from __future__ import annotations

import unittest
from unittest.mock import patch

from tplus_datahub.core.exceptions import ChanjetAPIError


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.rows = [(17,), (31,)]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=()):
        self.statements.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0)


class FakeConn:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.close_count += 1


class FailingConn(FakeConn):
    def cursor(self):
        raise RuntimeError("database unavailable")


class RollbackFailingConn(FailingConn):
    def rollback(self):
        self.rollback_count += 1
        raise RuntimeError("rollback unavailable")


class SyncJobPlatformTests(unittest.TestCase):
    def test_fixed_tplus_job_uses_null_source_and_refreshes_only_platform_fields(self):
        from tplus_datahub.jobs import sync_job_platform

        conn = FakeConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=conn):
            run_id = sync_job_platform.start_run(
                job_key="chanjet.full",
                kind="pull",
                provider="chanjet",
                display_name="T+ 全量同步",
                source_id=None,
                trigger="schedule",
                legacy_ref={},
            )

        self.assertEqual(31, run_id)
        self.assertEqual(1, conn.commit_count)
        self.assertEqual(1, conn.close_count)
        job_sql, job_params = conn.cursor_obj.statements[0]
        self.assertIn("updated_at = NOW()", job_sql)
        self.assertIn(None, job_params)
        for protected in ("schedule =", "freshness_sla_seconds =", "artifact_glob =", "alert_enabled =", "alert_chat_id ="):
            self.assertNotIn(protected, job_sql)

    def test_step_uses_run_sequence_conflict_target(self):
        from tplus_datahub.jobs import sync_job_platform

        conn = FakeConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=conn):
            sync_job_platform.upsert_step(31, 2, "fetch_page", "success", items=40)

        self.assertIn("ON CONFLICT (run_id, seq)", conn.cursor_obj.statements[0][0])
        self.assertEqual(1, conn.commit_count)
        self.assertEqual(1, conn.close_count)

    def test_attach_legacy_ref_only_updates_the_platform_run_reference(self):
        from tplus_datahub.jobs import sync_job_platform

        conn = FakeConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=conn):
            sync_job_platform.attach_legacy_ref(31, 88)

        sql, params = conn.cursor_obj.statements[0]
        self.assertEqual("UPDATE sync_job_runs\nSET legacy_ref = %s\nWHERE id = %s", "\n".join(line.strip() for line in sql.splitlines() if line.strip()))
        self.assertEqual(31, params[1])
        self.assertEqual(1, conn.commit_count)
        self.assertEqual(1, conn.close_count)

    def test_chanjet_http_statuses_map_to_platform_error_kinds(self):
        from tplus_datahub.jobs import sync_job_platform

        cases = ((401, "auth"), (429, "rate_limit"), (500, "network"))
        for status_code, expected in cases:
            error = ChanjetAPIError("upstream failed", endpoint="/example", status_code=status_code)
            self.assertEqual(expected, sync_job_platform.classify_error(error))

    def test_error_message_redacts_credentials_and_caps_length(self):
        from tplus_datahub.jobs import sync_job_platform

        message = sync_job_platform.safe_error_message(
            RuntimeError("Authorization: Bearer secret-value; access_token=token-value " + "x" * 600)
        )

        self.assertNotIn("secret-value", message)
        self.assertNotIn("token-value", message)
        self.assertLessEqual(len(message), 500)

    def test_missing_database_configuration_is_a_noop(self):
        from tplus_datahub.jobs import sync_job_platform

        with patch.object(sync_job_platform, "connect_if_configured", return_value=None):
            self.assertIsNone(sync_job_platform.start_run(
                job_key="chanjet.full", kind="pull", provider="chanjet", display_name="T+ 全量同步",
                source_id=None, trigger="schedule", legacy_ref={},
            ))
            self.assertIsNone(sync_job_platform.upsert_step(31, 1, "fetch", "running"))
            self.assertIsNone(sync_job_platform.finish_run(31, status="success", row_count=1, changed_count=1, error=None, detail_json={}))
            self.assertIsNone(sync_job_platform.attach_legacy_ref(31, 88))

    def test_connect_and_rollback_failures_are_fail_open(self):
        from tplus_datahub.jobs import sync_job_platform

        with patch.object(sync_job_platform, "connect_if_configured", side_effect=RuntimeError("connect failed")):
            self.assertIsNone(sync_job_platform.start_run(
                job_key="chanjet.full", kind="pull", provider="chanjet", display_name="T+ 全量同步",
                source_id=None, trigger="schedule", legacy_ref={},
            ))

        conn = RollbackFailingConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=conn):
            self.assertIsNone(sync_job_platform.start_run(
                job_key="chanjet.full", kind="pull", provider="chanjet", display_name="T+ 全量同步",
                source_id=None, trigger="schedule", legacy_ref={},
            ))
        self.assertEqual(1, conn.rollback_count)
        self.assertEqual(1, conn.close_count)


if __name__ == "__main__":
    unittest.main()
