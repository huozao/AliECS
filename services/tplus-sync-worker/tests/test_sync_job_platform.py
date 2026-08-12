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


class CommitFailingConn(FakeConn):
    def commit(self):
        self.commit_count += 1
        raise RuntimeError("commit unavailable")


class CloseFailingConn(FakeConn):
    def close(self):
        self.close_count += 1
        raise RuntimeError("close unavailable")


class RollbackCloseFailingConn(RollbackFailingConn):
    def close(self):
        self.close_count += 1
        raise RuntimeError("close unavailable")


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
        self.assertIsNone(job_params[4])
        for protected in ("schedule =", "freshness_sla_seconds =", "artifact_glob =", "alert_enabled =", "alert_chat_id ="):
            self.assertNotIn(protected, job_sql)

    def test_parent_match_is_another_fixed_job_with_null_source(self):
        from tplus_datahub.jobs import sync_job_platform

        conn = FakeConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=conn):
            run_id = sync_job_platform.start_run(
                job_key="tplus.parent_match",
                kind="reconcile",
                provider="chanjet",
                display_name="T+ 父件核对",
                source_id=None,
                trigger="schedule",
                legacy_ref={},
            )

        self.assertEqual(31, run_id)
        self.assertEqual("tplus.parent_match", conn.cursor_obj.statements[0][1][0])
        self.assertIsNone(conn.cursor_obj.statements[0][1][4])

    def test_start_run_rejects_unknown_jobs_and_non_null_sources_without_sql(self):
        from tplus_datahub.jobs import sync_job_platform

        invalid_cases = (("chanjet.full", 1), ("unknown.job", None))
        for job_key, source_id in invalid_cases:
            conn = FakeConn()
            with patch.object(sync_job_platform, "connect_if_configured", return_value=conn):
                result = sync_job_platform.start_run(
                    job_key=job_key,
                    kind="pull",
                    provider="chanjet",
                    display_name="T+ 全量同步",
                    source_id=source_id,
                    trigger="schedule",
                    legacy_ref={},
                )
            self.assertIsNone(result)
            self.assertEqual([], conn.cursor_obj.statements)
            self.assertEqual(1, conn.rollback_count)
            self.assertEqual(1, conn.close_count)

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
        self.assertEqual({"table": "integration_sync_runs", "id": 88}, params[0].obj)
        self.assertEqual(31, params[1])
        self.assertEqual(1, conn.commit_count)
        self.assertEqual(1, conn.close_count)

    def test_chanjet_http_statuses_map_to_platform_error_kinds(self):
        from tplus_datahub.jobs import sync_job_platform

        cases = (
            (ChanjetAPIError("upstream failed", endpoint="/example", status_code=401), "auth"),
            (ChanjetAPIError("upstream failed", endpoint="/example", status_code=429), "rate_limit"),
            (ChanjetAPIError("upstream failed", endpoint="/example", status_code=500), "network"),
            (RuntimeError("schema validation failed"), "schema"),
            (RuntimeError("database write constraint"), "write"),
            (RuntimeError("opaque failure"), "unknown"),
        )
        for error, expected in cases:
            self.assertEqual(expected, sync_job_platform.classify_error(error))

    def test_finish_run_persists_redacted_error_and_all_platform_fields(self):
        from tplus_datahub.jobs import sync_job_platform

        conn = FakeConn()
        error = ChanjetAPIError(
            "Authorization: Bearer secret-value; access_token=token-value",
            endpoint="/example",
            status_code=429,
        )
        detail = {"modules": ["bom"], "snapshot": 9}
        with patch.object(sync_job_platform, "connect_if_configured", return_value=conn):
            sync_job_platform.finish_run(
                31,
                status="partial",
                row_count=14,
                changed_count=5,
                error=error,
                detail_json=detail,
            )

        sql, params = conn.cursor_obj.statements[0]
        self.assertIn("UPDATE sync_job_runs", sql)
        for column in ("status = %s", "finished_at = NOW()", "row_count = %s", "changed_count = %s", "error_kind = %s", "error_message = %s", "detail_json = %s"):
            self.assertIn(column, sql)
        self.assertEqual("partial", params[0])
        self.assertEqual(14, params[1])
        self.assertEqual(5, params[2])
        self.assertEqual("rate_limit", params[3])
        self.assertNotIn("secret-value", params[4])
        self.assertNotIn("token-value", params[4])
        self.assertEqual(detail, params[5].obj)
        self.assertEqual(31, params[6])
        self.assertEqual(1, conn.commit_count)
        self.assertEqual(1, conn.close_count)

    def test_finish_run_persists_only_the_allowed_error_kind_values(self):
        from tplus_datahub.jobs import sync_job_platform

        cases = (
            (ChanjetAPIError("failed", endpoint="/example", status_code=401), "auth"),
            (ChanjetAPIError("failed", endpoint="/example", status_code=429), "rate_limit"),
            (ChanjetAPIError("failed", endpoint="/example", status_code=500), "network"),
            (RuntimeError("schema validation failed"), "schema"),
            (RuntimeError("database write constraint"), "write"),
            (RuntimeError("opaque failure"), "unknown"),
        )
        for error, expected_kind in cases:
            conn = FakeConn()
            with patch.object(sync_job_platform, "connect_if_configured", return_value=conn):
                sync_job_platform.finish_run(
                    31,
                    status="failed",
                    row_count=0,
                    changed_count=0,
                    error=error,
                    detail_json={},
                )
            self.assertEqual(expected_kind, conn.cursor_obj.statements[0][1][3])

    def test_invalid_run_and_step_statuses_do_not_write_sql(self):
        from tplus_datahub.jobs import sync_job_platform

        run_conn = FakeConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=run_conn):
            self.assertIsNone(sync_job_platform.finish_run(
                31,
                status="partial_failed",
                row_count=0,
                changed_count=0,
                error=None,
                detail_json={},
            ))
        self.assertEqual([], run_conn.cursor_obj.statements)
        self.assertEqual(1, run_conn.rollback_count)
        self.assertEqual(1, run_conn.close_count)

        step_conn = FakeConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=step_conn):
            self.assertIsNone(sync_job_platform.upsert_step(31, 1, "fetch", "partial"))
        self.assertEqual([], step_conn.cursor_obj.statements)
        self.assertEqual(1, step_conn.rollback_count)
        self.assertEqual(1, step_conn.close_count)

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

    def test_commit_failure_rolls_back_closes_and_does_not_propagate(self):
        from tplus_datahub.jobs import sync_job_platform

        conn = CommitFailingConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=conn):
            self.assertIsNone(sync_job_platform.upsert_step(31, 1, "fetch", "success"))

        self.assertEqual(1, conn.commit_count)
        self.assertEqual(1, conn.rollback_count)
        self.assertEqual(1, conn.close_count)

    def test_logger_failure_does_not_escape_a_platform_write_failure(self):
        from tplus_datahub.jobs import sync_job_platform

        conn = FailingConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=conn), patch("builtins.print", side_effect=RuntimeError("logger unavailable")):
            self.assertIsNone(sync_job_platform.upsert_step(31, 1, "fetch", "success"))

        self.assertEqual(1, conn.rollback_count)
        self.assertEqual(1, conn.close_count)

    def test_close_failure_does_not_escape_after_a_successful_write(self):
        from tplus_datahub.jobs import sync_job_platform

        conn = CloseFailingConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=conn):
            self.assertIsNone(sync_job_platform.upsert_step(31, 1, "fetch", "success"))

        self.assertEqual(1, conn.commit_count)
        self.assertEqual(1, conn.close_count)

    def test_rollback_logger_and_close_failures_together_are_fail_open(self):
        from tplus_datahub.jobs import sync_job_platform

        conn = RollbackCloseFailingConn()
        with patch.object(sync_job_platform, "connect_if_configured", return_value=conn), patch("builtins.print", side_effect=RuntimeError("logger unavailable")):
            self.assertIsNone(sync_job_platform.upsert_step(31, 1, "fetch", "success"))

        self.assertEqual(1, conn.rollback_count)
        self.assertEqual(1, conn.close_count)


if __name__ == "__main__":
    unittest.main()
