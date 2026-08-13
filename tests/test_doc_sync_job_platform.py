from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"
WRITER_PATH = WORKER_ROOT / "app" / "storage" / "sync_job_platform.py"


def _load_writer_module() -> Any:
    old_path = list(sys.path)
    old_app_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    try:
        spec = importlib.util.spec_from_file_location(
            "_doc_sync_job_platform_unit", WRITER_PATH
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load sync job writer from {WRITER_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = old_path
        for name in tuple(sys.modules):
            if (name == "app" or name.startswith("app.")) and name not in old_app_modules:
                sys.modules.pop(name, None)
        sys.modules.update(old_app_modules)


writer_module = _load_writer_module()
SyncJobPlatformWriter = writer_module.SyncJobPlatformWriter
classify_error = writer_module.classify_error
platform_writer_for = writer_module.platform_writer_for
safe_error_message = writer_module.safe_error_message


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self.conn.sql.append(sql)
        self.conn.params.append(params)

    def fetchone(self):
        return (31,)


class FakeConn:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.params: list[object] = []
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


class FailingRollbackConn(FailingConn):
    def rollback(self) -> None:
        self.rollback_count += 1
        raise RuntimeError("rollback failed")


class CatalogCursor(FakeCursor):
    def __init__(self, conn: "CatalogConn") -> None:
        super().__init__(conn)
        self.current: list[tuple[int, ...]] = []

    def execute(self, sql: str, params=None) -> None:
        super().execute(sql, params)
        self.current = self.conn.responses.pop(0) if self.conn.responses else []

    def fetchall(self):
        return list(self.current)


class CatalogConn(FakeConn):
    def __init__(self, responses: list[list[tuple[int, ...]]]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.catalog_cursor = CatalogCursor(self)

    def cursor(self) -> CatalogCursor:
        return self.catalog_cursor


class SyncJobPlatformWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = FakeConn()

    def test_isolated_loader_preserves_import_state(self):
        old_path = list(sys.path)
        old_app_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "app" or name.startswith("app.")
        }

        loaded = _load_writer_module()

        self.assertIsNotNone(loaded.SyncJobPlatformWriter)
        self.assertEqual(old_path, sys.path)
        self.assertEqual(
            old_app_modules,
            {
                name: module
                for name, module in sys.modules.items()
                if name == "app" or name.startswith("app.")
            },
        )

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

    def test_reconcile_upserts_only_active_table_sources_without_runs(self):
        conn = CatalogConn([[(17,), (19,)], [(23,)]])

        result = SyncJobPlatformWriter(conn).reconcile_document_jobs()

        self.assertEqual({"enabled": 2, "disabled": 1}, result)
        sql = "\n".join(conn.sql)
        self.assertIn("source_type = 'smartsheet_sheet'", sql)
        self.assertIn("source_type = 'bitable_table'", sql)
        self.assertIn("status = 'active'", sql)
        self.assertIn("ON CONFLICT(job_key) DO UPDATE", sql)
        self.assertNotIn("INSERT INTO sync_job_runs", sql)
        self.assertNotIn("smartsheet_link", sql)
        self.assertNotIn("structure_backup", sql)
        self.assertEqual(1, conn.commits)

    def test_reconcile_preserves_operator_schedule_and_updates_timestamp(self):
        conn = CatalogConn([[(17,)], []])

        SyncJobPlatformWriter(conn).reconcile_document_jobs()

        sql = "\n".join(conn.sql)
        self.assertIn("schedule = CASE", sql)
        self.assertIn("sync_jobs.schedule = '{}'::jsonb", sql)
        self.assertIn("ELSE sync_jobs.schedule", sql)
        self.assertGreaterEqual(sql.count("updated_at = NOW()"), 2)
        for protected in ("freshness_sla_seconds =", "artifact_glob =", "alert_enabled =", "alert_chat_id ="):
            self.assertNotIn(protected, sql)

    def test_reconcile_failure_rolls_back_and_is_fail_open(self):
        conn = FailingConn()

        result = SyncJobPlatformWriter(conn).reconcile_document_jobs()

        self.assertIsNone(result)
        self.assertEqual(1, conn.rollback_count)

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

    def test_error_redaction_handles_json_dict_and_query_value_forms(self):
        for raw in (
            '{"access_token": "secret-value"}',
            "{'corpsecret': 'secret-value'}",
            '{"Authorization": "Bearer secret-value"}',
            "app secret = secret-value?query=1",
        ):
            with self.subTest(raw=raw):
                safe = safe_error_message(RuntimeError(raw))
                self.assertNotIn("secret-value", safe)
                self.assertLessEqual(len(safe), 500)

    def test_platform_failure_stays_fail_open_when_rollback_or_logger_fails(self):
        for conn, logger in (
            (FailingRollbackConn(), print),
            (FailingConn(), lambda _: (_ for _ in ()).throw(RuntimeError("logger failed"))),
        ):
            with self.subTest(conn=type(conn).__name__):
                result = SyncJobPlatformWriter(conn, logger=logger).start_run(
                    job_key="wecom.doc.17", kind="pull", provider="wecom",
                    display_name="点检表", source_id=17, trigger="manual", legacy_ref={},
                )
                self.assertIsNone(result)

    def test_invalid_platform_boundary_inputs_do_not_write_sql(self):
        invalid_starts = (
            {"job_key": "wecom.doc.17", "source_id": None, "legacy_ref": {}},
            {"job_key": "wecom.doc.17", "source_id": 18, "legacy_ref": {}},
            {"job_key": "chanjet.full", "source_id": 17, "legacy_ref": {}},
            {"job_key": "wecom.doc.17", "source_id": 17, "legacy_ref": {"table": "sync_runs", "id": "91"}},
        )
        for values in invalid_starts:
            with self.subTest(values=values):
                conn = FakeConn()
                result = SyncJobPlatformWriter(conn, logger=lambda _: None).start_run(
                    kind="pull", provider="wecom", display_name="点检表", trigger="manual", **values,
                )
                self.assertIsNone(result)
                self.assertEqual([], conn.sql)
                self.assertEqual([], conn.params)

    def test_unknown_job_key_does_not_write_sql_or_parameters(self):
        conn = FakeConn()

        result = SyncJobPlatformWriter(conn, logger=lambda _: None).start_run(
            job_key="arbitrary.future.job",
            kind="pull",
            provider="wecom",
            display_name="future",
            source_id=None,
            trigger="manual",
            legacy_ref={},
        )

        self.assertIsNone(result)
        self.assertEqual([], conn.sql)
        self.assertEqual([], conn.params)

    def test_job_key_kind_and_provider_mismatches_do_not_write_sql(self):
        invalid_starts = (
            ("wecom.doc.17", "pull", "feishu", 17),
            ("wecom.doc.17", "reconcile", "wecom", 17),
            ("feishu.doc.19", "pull", "wecom", 19),
            ("feishu.doc.19", "reconcile", "feishu", 19),
            ("chanjet.full", "reconcile", "chanjet", None),
            ("chanjet.full", "pull", "wecom", None),
            ("tplus.parent_match", "pull", "chanjet", None),
            ("tplus.parent_match", "reconcile", "wecom", None),
        )
        for job_key, kind, provider, source_id in invalid_starts:
            with self.subTest(job_key=job_key, kind=kind, provider=provider):
                conn = FakeConn()
                result = SyncJobPlatformWriter(conn, logger=lambda _: None).start_run(
                    job_key=job_key,
                    kind=kind,
                    provider=provider,
                    display_name="job",
                    source_id=source_id,
                    trigger="manual",
                    legacy_ref={},
                )
                self.assertIsNone(result)
                self.assertEqual([], conn.sql)
                self.assertEqual([], conn.params)

    def test_all_supported_job_key_metadata_combinations_still_start(self):
        valid_starts = (
            ("wecom.doc.17", "pull", "wecom", 17),
            ("feishu.doc.19", "pull", "feishu", 19),
            ("chanjet.full", "pull", "chanjet", None),
            ("tplus.parent_match", "reconcile", "chanjet", None),
        )
        for job_key, kind, provider, source_id in valid_starts:
            with self.subTest(job_key=job_key):
                conn = FakeConn()
                result = SyncJobPlatformWriter(conn).start_run(
                    job_key=job_key,
                    kind=kind,
                    provider=provider,
                    display_name="job",
                    source_id=source_id,
                    trigger="manual",
                    legacy_ref={},
                )
                self.assertEqual(31, result)
                self.assertEqual(2, len(conn.sql))
                self.assertEqual(2, len(conn.params))

    def test_invalid_run_or_step_status_does_not_write_sql(self):
        conn = FakeConn()
        writer = SyncJobPlatformWriter(conn, logger=lambda _: None)
        writer.finish_run(31, status="queued", row_count=0, changed_count=0, error=None, detail_json={})
        writer.upsert_step(31, 2, "fetch_page", "partial")
        self.assertEqual([], conn.sql)
        self.assertEqual([], conn.params)

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
