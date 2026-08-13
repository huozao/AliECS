from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"
DOC_ROOT = ROOT / "services" / "doc-sync-worker"
TPLUS_ROOT = ROOT / "services" / "tplus-sync-worker" / "src"

SCHEDULE = {"enabled": True, "interval_seconds": 21600, "anchor_time": "02:00"}
SHADOW_PAYLOAD = {
    "sampled_at": "2026-08-13T11:00:00+00:00",
    "legacy": {"due": "2026-08-13T12:00:00+00:00", "run_full": False, "wait_seconds": 3600},
    "candidate": {"due": "2026-08-13T12:00:00+00:00", "run_full": False, "wait_seconds": 3600},
    "decision_match": True,
    "due_delta_seconds": 0.0,
}


def _normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def _json_value(value: object) -> object:
    return getattr(value, "obj", value)


class _Cursor:
    def __init__(self, conn: "_Connection", *, returned_rows: list[tuple[object, ...]] | None = None) -> None:
        self.conn = conn
        self.returned_rows = list(returned_rows or [])
        self.last_sql = ""

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        if self.conn.fail_on and self.conn.fail_on in _normalized(sql):
            raise RuntimeError("database unavailable")
        self.last_sql = sql
        self.conn.statements.append((sql, params))

    def fetchone(self) -> tuple[object, ...] | None:
        if "select enabled" in _normalized(self.last_sql):
            return (True, 21600, "02:00", False, None, "ops-admin")
        return self.returned_rows.pop(0) if self.returned_rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows = list(self.returned_rows)
        self.returned_rows.clear()
        return rows


class _Connection:
    def __init__(self, *, returned_rows: list[tuple[object, ...]] | None = None, fail_on: str = "") -> None:
        self.cursor_obj = _Cursor(self, returned_rows=returned_rows)
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_on = fail_on

    def cursor(self) -> _Cursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


class BackendDualWriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.old_path = list(sys.path)
        cls.old_env = {key: os.environ.get(key) for key in ("AUTH_TOKEN_SECRET", "DATABASE_URL")}
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        os.environ["AUTH_TOKEN_SECRET"] = "sync-schedule-test-secret"
        os.environ["DATABASE_URL"] = "postgresql://unit-test/not-used"
        from app.core import _encode_token
        from app.main import app
        from app.routers import ops
        from fastapi.testclient import TestClient

        cls._encode_token = staticmethod(_encode_token)
        cls.app = app
        cls.ops = ops
        cls.TestClient = TestClient

    @classmethod
    def tearDownClass(cls) -> None:
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = cls.old_path

    def _headers(self) -> dict[str, str]:
        token = self._encode_token(
            {"sub": "ops-admin", "roles": ["admin"], "permissions": ["admin.access"], "exp": int(time.time()) + 3600}
        )
        return {"Authorization": f"Bearer {token}"}

    def test_tplus_put_dual_writes_schedule_in_one_transaction(self) -> None:
        conn = _Connection()
        with patch.object(self.ops, "_conn", return_value=conn):
            response = self.TestClient(self.app).put("/v1/ops/tplus/sync-config", json={"enabled": True, "interval_hours": 6, "anchor_time": "02:00"}, headers=self._headers())

        self.assertEqual(200, response.status_code)
        sql = "\n".join(statement for statement, _params in conn.statements)
        self.assertIn("UPDATE sync_jobs", sql)
        self.assertIn("job_key = 'chanjet.full'", sql)
        self.assertEqual(1, conn.commits)
        schedule_params = next(params for statement, params in conn.statements if "UPDATE sync_jobs" in statement)
        self.assertEqual(SCHEDULE, _json_value(schedule_params[0]))

    def test_doc_put_dual_writes_only_document_pull_jobs(self) -> None:
        conn = _Connection()
        with patch.object(self.ops, "_conn", return_value=conn):
            response = self.TestClient(self.app).put(
                "/v1/ops/doc-sync/sync-config",
                json={"enabled": True, "interval_hours": 6, "anchor_time": "02:00", "pull_paused": False},
                headers=self._headers(),
            )

        self.assertEqual(200, response.status_code)
        normalized = "\n".join(_normalized(statement) for statement, _params in conn.statements)
        self.assertIn("update sync_jobs", normalized)
        self.assertIn("kind = 'pull'", normalized)
        self.assertIn("provider in ('wecom', 'feishu')", normalized)
        self.assertEqual(1, conn.commits)

    def test_backend_rolls_back_both_writes_when_platform_update_fails(self) -> None:
        conn = _Connection(fail_on="update sync_jobs")
        with patch.object(self.ops, "_conn", return_value=conn):
            response = self.TestClient(self.app, raise_server_exceptions=False).put(
                "/v1/ops/tplus/sync-config",
                json={"enabled": True, "interval_hours": 6, "anchor_time": "02:00"},
                headers=self._headers(),
            )

        self.assertEqual(500, response.status_code)
        self.assertEqual(0, conn.commits)
        self.assertEqual(1, conn.rollbacks)


class DocScheduleStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.old_path = list(sys.path)
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(DOC_ROOT))
        from app.pipelines import sync_schedule
        from app.storage.postgres import PostgresDocSyncStore

        cls.sync_schedule = sync_schedule
        cls.PostgresDocSyncStore = PostgresDocSyncStore

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = cls.old_path

    def test_doc_legacy_upsert_seeds_empty_platform_schedule_without_overwriting_nonempty(self) -> None:
        conn = _Connection()

        self.PostgresDocSyncStore(conn).upsert_sync_config("doc_sync", **SCHEDULE, updated_by="feishu-config-table")

        self.assertEqual(1, conn.commits)
        normalized = "\n".join(_normalized(statement) for statement, _params in conn.statements)
        self.assertIn("insert into integration_sync_config", normalized)
        self.assertIn("update sync_jobs", normalized)
        self.assertIn("kind = 'pull'", normalized)
        self.assertIn("provider in ('wecom', 'feishu')", normalized)
        self.assertIn("schedule = '{}'::jsonb", normalized)
        self.assertIn("updated_at = now()", normalized)
        schedule_params = next(params for statement, params in conn.statements if "UPDATE sync_jobs" in statement)
        self.assertEqual(SCHEDULE, _json_value(schedule_params[0]))

    def test_doc_read_platform_schedule_is_fail_open(self) -> None:
        class Store:
            closed = 0

            def fetch_platform_schedule(self) -> dict[str, object]:
                return SCHEDULE

            def close(self) -> None:
                self.closed += 1

        store = Store()
        with patch.object(self.sync_schedule, "open_store", return_value=store):
            self.assertEqual(SCHEDULE, self.sync_schedule.read_platform_schedule())
        self.assertEqual(1, store.closed)

    def test_doc_shadow_updates_latest_real_schedule_runs_only(self) -> None:
        conn = _Connection(returned_rows=[(101,), (102,)])
        store = self.PostgresDocSyncStore(conn)

        run_ids = store.record_scheduler_shadow(SHADOW_PAYLOAD)

        self.assertEqual([101, 102], run_ids)
        normalized = _normalized(conn.statements[0][0])
        self.assertIn("trigger = 'schedule'", normalized)
        self.assertIn("j.kind = 'pull'", normalized)
        self.assertIn("j.provider in ('wecom', 'feishu')", normalized)
        self.assertNotIn("insert into sync_job_runs", normalized)
        self.assertEqual(SHADOW_PAYLOAD, _json_value(conn.statements[0][1][0]))
        self.assertEqual(1, conn.commits)

    def test_doc_shadow_finishes_only_the_recorded_run_ids(self) -> None:
        conn = _Connection()
        store = self.PostgresDocSyncStore(conn)

        store.finish_scheduler_shadow([101, 102], observed_sleep_seconds=17, candidate_would_wake=True)

        normalized = _normalized(conn.statements[0][0])
        self.assertIn("where r.id = any(%s)", normalized)
        self.assertNotIn("distinct on", normalized)
        self.assertNotIn("insert into sync_job_runs", normalized)
        self.assertEqual((17, True, [101, 102]), conn.statements[0][1])
        self.assertEqual(1, conn.commits)

    def test_doc_shadow_failure_rolls_back_and_returns_empty_ids(self) -> None:
        conn = _Connection(fail_on="update sync_job_runs")

        self.assertEqual([], self.PostgresDocSyncStore(conn).record_scheduler_shadow(SHADOW_PAYLOAD))

        self.assertEqual(0, conn.commits)
        self.assertEqual(1, conn.rollbacks)


class TplusScheduleStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.old_path = list(sys.path)
        sys.path.insert(0, str(TPLUS_ROOT))
        from tplus_datahub.jobs import db_sync_requests

        cls.module = db_sync_requests

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = cls.old_path

    def test_tplus_platform_schedule_uses_only_chanjet_full_job(self) -> None:
        conn = _Connection(returned_rows=[(SCHEDULE,)])

        self.assertEqual(SCHEDULE, self.module.fetch_platform_schedule(conn=conn))
        normalized = _normalized(conn.statements[0][0])
        self.assertIn("from sync_jobs", normalized)
        self.assertIn("job_key = %s", normalized)
        self.assertEqual(("chanjet.full",), conn.statements[0][1])

    def test_tplus_seed_only_updates_empty_platform_schedule(self) -> None:
        conn = _Connection()

        self.module.seed_platform_schedule(SCHEDULE, conn=conn)

        normalized = _normalized(conn.statements[0][0])
        self.assertIn("update sync_jobs", normalized)
        self.assertIn("job_key = %s", normalized)
        self.assertIn("schedule = '{}'::jsonb", normalized)
        self.assertIn("updated_at = now()", normalized)
        self.assertEqual((SCHEDULE, "chanjet.full"), tuple(_json_value(value) for value in conn.statements[0][1]))
        self.assertEqual(1, conn.commits)

    def test_tplus_shadow_updates_latest_real_schedule_run_and_returns_its_id(self) -> None:
        conn = _Connection(returned_rows=[(301,)])

        run_ids = self.module.record_scheduler_shadow(SHADOW_PAYLOAD, conn=conn)

        self.assertEqual([301], run_ids)
        normalized = _normalized(conn.statements[0][0])
        self.assertIn("trigger = 'schedule'", normalized)
        self.assertIn("j.job_key = %s", normalized)
        self.assertNotIn("insert into sync_job_runs", normalized)
        self.assertEqual(("chanjet.full", SHADOW_PAYLOAD), tuple(_json_value(value) for value in conn.statements[0][1]))

    def test_tplus_shadow_finish_uses_recorded_ids_and_failure_is_fail_open(self) -> None:
        conn = _Connection()

        self.module.finish_scheduler_shadow([301], observed_sleep_seconds=31, candidate_would_wake=False, conn=conn)

        normalized = _normalized(conn.statements[0][0])
        self.assertIn("where r.id = any(%s)", normalized)
        self.assertNotIn("distinct on", normalized)
        self.assertEqual((31, False, [301]), conn.statements[0][1])

        failing = _Connection(fail_on="update sync_job_runs")
        self.assertEqual([], self.module.record_scheduler_shadow(SHADOW_PAYLOAD, conn=failing))
        self.assertEqual(1, failing.rollbacks)


if __name__ == "__main__":
    unittest.main()
