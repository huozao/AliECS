from __future__ import annotations

import os
import sys
import time
import unittest
from copy import deepcopy
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
        normalized = _normalized(self.last_sql)
        if "update sync_jobs" in normalized and "returning job_key" in normalized:
            return [("chanjet.full",)]
        if "update sync_jobs" in normalized and "returning id" in normalized:
            return [(1,), (2,)]
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


class _StatefulCursor:
    def __init__(self, conn: "_StatefulConnection") -> None:
        self.conn = conn
        self.rows: list[tuple[object, ...]] = []
        self.rowcount = -1

    def __enter__(self) -> "_StatefulCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        normalized = _normalized(sql)
        self.conn.statements.append((sql, params))
        if self.conn.aborted and not normalized.startswith("rollback to savepoint "):
            raise RuntimeError("current transaction is aborted")
        if self.conn.fail_once and self.conn.fail_once in normalized:
            self.conn.fail_once = ""
            self.conn.aborted = True
            raise RuntimeError("database unavailable")
        if self.conn.fail_on and self.conn.fail_on in normalized:
            self.conn.aborted = True
            raise RuntimeError("database unavailable")
        self.rows = []
        self.rowcount = -1
        if normalized.startswith("savepoint "):
            name = normalized.split()[-1]
            self.conn.savepoints[name] = deepcopy(self.conn.working)
            return
        if normalized.startswith("rollback to savepoint "):
            name = normalized.split()[-1]
            self.conn.working = deepcopy(self.conn.savepoints[name])
            self.conn.aborted = False
            return
        if normalized.startswith("release savepoint "):
            self.conn.savepoints.pop(normalized.split()[-1], None)
            return
        if "insert into integration_sync_config" in normalized:
            if "values ('chanjet'" in normalized:
                provider, values = "chanjet", params
            elif "values ('doc_sync'" in normalized:
                provider, values = "doc_sync", params
            else:
                provider, values = str(params[0]), params[1:]
            config = {
                "enabled": bool(values[0]),
                "interval_seconds": int(values[1]),
                "anchor_time": str(values[2] or ""),
                "pull_paused": bool(values[3]) if provider == "doc_sync" and "pull_paused" in normalized else False,
                "updated_by": str(values[-1]),
            }
            self.conn.working["configs"][provider] = config
            self.rowcount = 1
            return
        if normalized.startswith("select enabled"):
            provider = "doc_sync" if "provider = 'doc_sync'" in normalized else str(params[0])
            config = self.conn.working["configs"].get(provider)
            if config is not None:
                if "pull_paused" in normalized:
                    self.rows = [
                        (
                            config["enabled"],
                            config["interval_seconds"],
                            config["anchor_time"],
                            config["pull_paused"],
                            None,
                            config["updated_by"],
                        )
                    ]
                else:
                    self.rows = [
                        (
                            config["enabled"],
                            config["interval_seconds"],
                            config["anchor_time"],
                            None,
                            config["updated_by"],
                        )
                    ]
            return
        if normalized.startswith("select schedule from sync_jobs"):
            if "job_key = %s" in normalized:
                jobs = [job for job in self.conn.working["jobs"] if job["job_key"] == params[0]]
            else:
                jobs = [
                    job
                    for job in self.conn.working["jobs"]
                    if job["kind"] == "pull" and job["provider"] in {"wecom", "feishu"} and job["schedule"]
                ]
            self.rows = [(deepcopy(jobs[0]["schedule"]),)] if jobs else []
            return
        if normalized.startswith("update sync_jobs"):
            if "job_key = 'chanjet.full'" in normalized:
                jobs = [job for job in self.conn.working["jobs"] if job["job_key"] == "chanjet.full"]
            elif "job_key = %s" in normalized:
                jobs = [job for job in self.conn.working["jobs"] if job["job_key"] == params[-1]]
            else:
                jobs = [
                    job
                    for job in self.conn.working["jobs"]
                    if job["kind"] == "pull" and job["provider"] in {"wecom", "feishu"}
                ]
            if "schedule = '{}'::jsonb" in normalized:
                jobs = [job for job in jobs if job["schedule"] == {}]
            schedule = deepcopy(_json_value(params[0]))
            for job in jobs:
                job["schedule"] = schedule
            self.rowcount = len(jobs)
            if "returning job_key" in normalized:
                self.rows = [(job["job_key"],) for job in jobs]
            elif "returning id" in normalized:
                self.rows = [(job["id"],) for job in jobs]
            return
        if normalized.startswith("with latest as"):
            if "j.job_key = %s" in normalized:
                jobs = {job["id"]: job for job in self.conn.working["jobs"] if job["job_key"] == params[0]}
                payload = deepcopy(_json_value(params[1]))
            else:
                jobs = {
                    job["id"]: job
                    for job in self.conn.working["jobs"]
                    if job["kind"] == "pull" and job["provider"] in {"wecom", "feishu"}
                }
                payload = deepcopy(_json_value(params[0]))
            latest: list[dict[str, object]] = []
            for job_id in jobs:
                matches = [
                    run
                    for run in self.conn.working["runs"]
                    if run["job_id"] == job_id and run["trigger"] == "schedule"
                ]
                if matches:
                    if "r.id desc" in normalized:
                        matches.sort(key=lambda run: (str(run["started_at"]), int(run["id"])), reverse=True)
                    else:
                        matches.sort(key=lambda run: str(run["started_at"]), reverse=True)
                    latest.append(matches[0])
            for run in latest:
                run["detail_json"]["shadow"] = deepcopy(payload)
            self.rows = [(run["id"],) for run in latest]
            self.rowcount = len(latest)
            return
        if normalized.startswith("update sync_job_runs r"):
            observed_sleep_seconds, candidate_would_wake, run_ids = params
            for run in self.conn.working["runs"]:
                if run["id"] in run_ids:
                    if "coalesce" in normalized:
                        shadow = deepcopy(run["detail_json"].get("shadow") or {})
                        shadow.update(
                            {
                                "observed_sleep_seconds": int(observed_sleep_seconds),
                                "candidate_would_wake": bool(candidate_would_wake),
                            }
                        )
                        run["detail_json"]["shadow"] = shadow
                    else:
                        run["detail_json"]["shadow"] = {
                            "observed_sleep_seconds": int(observed_sleep_seconds),
                            "candidate_would_wake": bool(candidate_would_wake),
                        }
            return
        raise AssertionError(f"unmodeled SQL: {sql}")

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows = list(self.rows)
        self.rows.clear()
        return rows


class _StatefulConnection:
    def __init__(
        self,
        *,
        jobs: list[dict[str, object]] | None = None,
        runs: list[dict[str, object]] | None = None,
        configs: dict[str, dict[str, object]] | None = None,
        fail_on: str = "",
        fail_once: str = "",
    ) -> None:
        self.persistent = {
            "jobs": deepcopy(jobs or []),
            "runs": deepcopy(runs or []),
            "configs": deepcopy(configs or {}),
        }
        self.working = deepcopy(self.persistent)
        self.fail_on = fail_on
        self.fail_once = fail_once
        self.aborted = False
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.savepoints: dict[str, dict[str, object]] = {}

    def cursor(self) -> _StatefulCursor:
        return _StatefulCursor(self)

    def commit(self) -> None:
        self.persistent = deepcopy(self.working)
        self.commits += 1

    def rollback(self) -> None:
        self.working = deepcopy(self.persistent)
        self.aborted = False
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

    def test_tplus_zero_platform_target_rolls_back_legacy_state(self) -> None:
        conn = _StatefulConnection(
            configs={
                "chanjet": {
                    "enabled": False,
                    "interval_seconds": 86400,
                    "anchor_time": "",
                    "pull_paused": False,
                    "updated_by": "before",
                }
            }
        )
        before = deepcopy(conn.persistent)

        with patch.object(self.ops, "_conn", return_value=conn):
            response = self.TestClient(self.app, raise_server_exceptions=False).put(
                "/v1/ops/tplus/sync-config",
                json={"enabled": True, "interval_hours": 6, "anchor_time": "02:00"},
                headers=self._headers(),
            )

        self.assertEqual(500, response.status_code)
        self.assertEqual(before, conn.persistent)
        self.assertEqual(0, conn.commits)
        self.assertEqual(1, conn.rollbacks)

    def test_doc_put_updates_every_matching_job_and_zero_target_rolls_back(self) -> None:
        jobs = [
            {"id": 1, "job_key": "wecom.one", "kind": "pull", "provider": "wecom", "schedule": {}},
            {"id": 2, "job_key": "feishu.one", "kind": "pull", "provider": "feishu", "schedule": {}},
            {"id": 3, "job_key": "wecom.write", "kind": "writeback", "provider": "wecom", "schedule": {}},
        ]
        conn = _StatefulConnection(jobs=jobs)
        with patch.object(self.ops, "_conn", return_value=conn):
            response = self.TestClient(self.app).put(
                "/v1/ops/doc-sync/sync-config",
                json={"enabled": True, "interval_hours": 6, "anchor_time": "02:00", "pull_paused": False},
                headers=self._headers(),
            )

        self.assertEqual(200, response.status_code)
        schedules = {job["job_key"]: job["schedule"] for job in conn.persistent["jobs"]}
        self.assertEqual(SCHEDULE, schedules["wecom.one"])
        self.assertEqual(SCHEDULE, schedules["feishu.one"])
        self.assertEqual({}, schedules["wecom.write"])

        zero_target = _StatefulConnection(
            configs={
                "doc_sync": {
                    "enabled": False,
                    "interval_seconds": 86400,
                    "anchor_time": "",
                    "pull_paused": False,
                    "updated_by": "before",
                }
            }
        )
        before = deepcopy(zero_target.persistent)
        with patch.object(self.ops, "_conn", return_value=zero_target):
            response = self.TestClient(self.app, raise_server_exceptions=False).put(
                "/v1/ops/doc-sync/sync-config",
                json={"enabled": True, "interval_hours": 6, "anchor_time": "02:00", "pull_paused": False},
                headers=self._headers(),
            )

        self.assertEqual(500, response.status_code)
        self.assertEqual(before, zero_target.persistent)
        self.assertEqual(1, zero_target.rollbacks)


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

    def test_doc_seed_changes_only_empty_schedule_and_shadow_finishes_returned_ids(self) -> None:
        jobs = [
            {"id": 1, "job_key": "wecom.one", "kind": "pull", "provider": "wecom", "schedule": {}},
            {"id": 2, "job_key": "feishu.one", "kind": "pull", "provider": "feishu", "schedule": {"enabled": False}},
        ]
        runs = [
            {"id": 101, "job_id": 1, "trigger": "schedule", "started_at": "2026-08-13T11:00:00+00:00", "detail_json": {}},
            {"id": 102, "job_id": 2, "trigger": "schedule", "started_at": "2026-08-13T11:00:00+00:00", "detail_json": {}},
            {"id": 103, "job_id": 1, "trigger": "schedule", "started_at": "2026-08-13T12:00:00+00:00", "detail_json": {}},
        ]
        conn = _StatefulConnection(jobs=jobs, runs=runs)
        store = self.PostgresDocSyncStore(conn)

        store.seed_platform_schedule(SCHEDULE)
        self.assertEqual(SCHEDULE, conn.persistent["jobs"][0]["schedule"])
        self.assertEqual({"enabled": False}, conn.persistent["jobs"][1]["schedule"])

        run_ids = store.record_scheduler_shadow(SHADOW_PAYLOAD)
        self.assertEqual([103, 102], run_ids)
        later_run = {run["id"]: run for run in conn.persistent["runs"]}[103]
        self.assertEqual(SHADOW_PAYLOAD, later_run["detail_json"]["shadow"])
        conn.working["runs"].append(
            {"id": 104, "job_id": 1, "trigger": "schedule", "started_at": "2026-08-13T13:00:00+00:00", "detail_json": {}}
        )

        store.finish_scheduler_shadow(run_ids, observed_sleep_seconds=17, candidate_would_wake=True)
        by_id = {run["id"]: run for run in conn.persistent["runs"]}
        self.assertEqual(17, by_id[103]["detail_json"]["shadow"]["observed_sleep_seconds"])
        self.assertTrue(by_id[103]["detail_json"]["shadow"]["candidate_would_wake"])
        self.assertEqual(SHADOW_PAYLOAD["legacy"], by_id[103]["detail_json"]["shadow"]["legacy"])
        self.assertNotIn("shadow", by_id[104]["detail_json"])

    def test_doc_latest_tie_chooses_highest_run_id(self) -> None:
        conn = _StatefulConnection(
            jobs=[{"id": 1, "job_key": "wecom.one", "kind": "pull", "provider": "wecom", "schedule": SCHEDULE}],
            runs=[
                {"id": 101, "job_id": 1, "trigger": "schedule", "started_at": "2026-08-13T11:00:00+00:00", "detail_json": {}},
                {"id": 102, "job_id": 1, "trigger": "schedule", "started_at": "2026-08-13T11:00:00+00:00", "detail_json": {}},
            ],
        )

        run_ids = self.PostgresDocSyncStore(conn).record_scheduler_shadow(SHADOW_PAYLOAD)

        self.assertEqual([102], run_ids)
        by_id = {run["id"]: run for run in conn.persistent["runs"]}
        self.assertNotIn("shadow", by_id[101]["detail_json"])
        self.assertEqual(SHADOW_PAYLOAD, by_id[102]["detail_json"]["shadow"])


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
        statement, params = next((item for item in conn.statements if "SELECT schedule" in item[0]))
        normalized = _normalized(statement)
        self.assertIn("from sync_jobs", normalized)
        self.assertIn("job_key = %s", normalized)
        self.assertEqual(("chanjet.full",), params)

    def test_tplus_borrowed_read_failure_restores_transaction_and_legacy_sql_can_continue(self) -> None:
        conn = _StatefulConnection(
            jobs=[{"id": 1, "job_key": "chanjet.full", "kind": "pull", "provider": "chanjet", "schedule": SCHEDULE}],
            fail_once="select schedule from sync_jobs",
        )
        conn.working["outer_legacy_write"] = {"request_id": 9}

        self.assertIsNone(self.module.fetch_platform_schedule(conn=conn))

        self.assertFalse(conn.aborted)
        self.assertEqual({"request_id": 9}, conn.working["outer_legacy_write"])
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO integration_sync_config(provider, enabled, interval_seconds, anchor_time, updated_at, updated_by) VALUES (%s, %s, %s, %s, NOW(), %s)",
                ("chanjet", True, 3600, "", "legacy-worker"),
            )
        self.assertEqual(3600, conn.working["configs"]["chanjet"]["interval_seconds"])
        self.assertEqual(0, conn.commits)
        self.assertEqual(0, conn.rollbacks)
        statements = "\n".join(statement for statement, _params in conn.statements).lower()
        self.assertIn("savepoint", statements)
        self.assertIn("rollback to savepoint", statements)
        self.assertIn("release savepoint", statements)

    def test_tplus_borrowed_read_release_failure_is_recovered_before_fallback(self) -> None:
        conn = _StatefulConnection(
            jobs=[{"id": 1, "job_key": "chanjet.full", "kind": "pull", "provider": "chanjet", "schedule": SCHEDULE}],
            fail_once="release savepoint",
        )

        self.assertIsNone(self.module.fetch_platform_schedule(conn=conn))

        self.assertFalse(conn.aborted)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO integration_sync_config(provider, enabled, interval_seconds, anchor_time, updated_at, updated_by) VALUES (%s, %s, %s, %s, NOW(), %s)",
                ("chanjet", True, 3600, "", "legacy-worker"),
            )
        statements = "\n".join(statement for statement, _params in conn.statements).lower()
        self.assertGreaterEqual(statements.count("release savepoint"), 2)
        self.assertIn("rollback to savepoint", statements)
        self.assertEqual(0, conn.commits)
        self.assertEqual(0, conn.rollbacks)

    def test_tplus_borrowed_savepoint_or_cleanup_failure_requires_connection_discard(self) -> None:
        cases = (
            ("savepoint creation", "savepoint tplus_scheduler_shadow", ""),
            ("rollback-to cleanup", "select schedule from sync_jobs", "rollback to savepoint"),
            ("release cleanup", "select schedule from sync_jobs", "release savepoint"),
        )
        for name, fail_once, fail_on in cases:
            with self.subTest(name=name):
                conn = _StatefulConnection(
                    jobs=[{"id": 1, "job_key": "chanjet.full", "kind": "pull", "provider": "chanjet", "schedule": SCHEDULE}],
                    fail_once=fail_once,
                    fail_on=fail_on,
                )

                with self.assertRaisesRegex(RuntimeError, "borrowed connection.*discard"):
                    self.module.fetch_platform_schedule(conn=conn)

                self.assertTrue(conn.aborted)
                self.assertEqual(0, conn.commits)
                self.assertEqual(0, conn.rollbacks)

    def test_tplus_owned_read_failure_rolls_back_and_returns_fallback(self) -> None:
        conn = _StatefulConnection(
            jobs=[{"id": 1, "job_key": "chanjet.full", "kind": "pull", "provider": "chanjet", "schedule": SCHEDULE}],
            fail_once="select schedule from sync_jobs",
        )
        with patch.object(self.module, "connect_if_configured", return_value=conn):
            self.assertIsNone(self.module.fetch_platform_schedule())

        self.assertFalse(conn.aborted)
        self.assertEqual(1, conn.rollbacks)
        self.assertEqual(0, conn.commits)

    def test_tplus_seed_only_updates_empty_platform_schedule(self) -> None:
        conn = _Connection()

        self.module.seed_platform_schedule(SCHEDULE, conn=conn)

        statement, params = next((item for item in conn.statements if "UPDATE sync_jobs" in item[0]))
        normalized = _normalized(statement)
        self.assertIn("update sync_jobs", normalized)
        self.assertIn("job_key = %s", normalized)
        self.assertIn("schedule = '{}'::jsonb", normalized)
        self.assertIn("updated_at = now()", normalized)
        self.assertEqual((SCHEDULE, "chanjet.full"), tuple(_json_value(value) for value in params))
        self.assertEqual(0, conn.commits)

    def test_tplus_shadow_updates_latest_real_schedule_run_and_returns_its_id(self) -> None:
        conn = _Connection(returned_rows=[(301,)])

        run_ids = self.module.record_scheduler_shadow(SHADOW_PAYLOAD, conn=conn)

        self.assertEqual([301], run_ids)
        statement, params = next((item for item in conn.statements if "WITH latest" in item[0]))
        normalized = _normalized(statement)
        self.assertIn("trigger = 'schedule'", normalized)
        self.assertIn("j.job_key = %s", normalized)
        self.assertNotIn("insert into sync_job_runs", normalized)
        self.assertEqual(("chanjet.full", SHADOW_PAYLOAD), tuple(_json_value(value) for value in params))

    def test_tplus_shadow_finish_uses_recorded_ids_and_failure_is_fail_open(self) -> None:
        conn = _Connection()

        self.module.finish_scheduler_shadow([301], observed_sleep_seconds=31, candidate_would_wake=False, conn=conn)

        statement, params = next((item for item in conn.statements if "UPDATE sync_job_runs" in item[0]))
        normalized = _normalized(statement)
        self.assertIn("where r.id = any(%s)", normalized)
        self.assertNotIn("distinct on", normalized)
        self.assertEqual((31, False, [301]), params)

        failing = _Connection(fail_on="update sync_job_runs")
        self.assertEqual([], self.module.record_scheduler_shadow(SHADOW_PAYLOAD, conn=failing))
        self.assertEqual(0, failing.rollbacks)

    def test_tplus_borrowed_connection_never_commits_or_rolls_back_outer_work(self) -> None:
        conn = _StatefulConnection(
            jobs=[{"id": 1, "job_key": "chanjet.full", "kind": "pull", "provider": "chanjet", "schedule": {}}],
            runs=[{"id": 301, "job_id": 1, "trigger": "schedule", "started_at": "2026-08-13T11:00:00+00:00", "detail_json": {}}],
        )
        conn.working["outer_legacy_write"] = {"request_id": 9}

        self.module.seed_platform_schedule(SCHEDULE, conn=conn)

        self.assertEqual(0, conn.commits)
        self.assertEqual(0, conn.rollbacks)
        self.assertEqual(SCHEDULE, conn.working["jobs"][0]["schedule"])
        self.assertEqual({}, conn.persistent["jobs"][0]["schedule"])

        conn.fail_on = "with latest"
        self.assertEqual([], self.module.record_scheduler_shadow(SHADOW_PAYLOAD, conn=conn))

        self.assertEqual({"request_id": 9}, conn.working["outer_legacy_write"])
        self.assertEqual(0, conn.commits)
        self.assertEqual(0, conn.rollbacks)
        statements = "\n".join(statement for statement, _params in conn.statements).lower()
        self.assertIn("savepoint", statements)
        self.assertIn("rollback to savepoint", statements)
        self.assertIn("release savepoint", statements)

    def test_tplus_latest_tie_chooses_highest_id_and_finish_merges_shadow_json(self) -> None:
        conn = _StatefulConnection(
            jobs=[{"id": 1, "job_key": "chanjet.full", "kind": "pull", "provider": "chanjet", "schedule": SCHEDULE}],
            runs=[
                {"id": 301, "job_id": 1, "trigger": "schedule", "started_at": "2026-08-13T11:00:00+00:00", "detail_json": {}},
                {"id": 302, "job_id": 1, "trigger": "schedule", "started_at": "2026-08-13T11:00:00+00:00", "detail_json": {}},
            ],
        )

        run_ids = self.module.record_scheduler_shadow(SHADOW_PAYLOAD, conn=conn)
        self.assertEqual([302], run_ids)
        self.module.finish_scheduler_shadow(run_ids, observed_sleep_seconds=31, candidate_would_wake=False, conn=conn)

        shadow = {run["id"]: run for run in conn.working["runs"]}[302]["detail_json"]["shadow"]
        self.assertEqual(SHADOW_PAYLOAD["candidate"], shadow["candidate"])
        self.assertEqual(31, shadow["observed_sleep_seconds"])
        self.assertFalse(shadow["candidate_would_wake"])


if __name__ == "__main__":
    unittest.main()
