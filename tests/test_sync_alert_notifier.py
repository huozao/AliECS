from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


class RecordingCursor:
    def __init__(self, conn: "RecordingConnection") -> None:
        self.conn = conn
        self.rowcount = 0

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        if self.conn.execute_error is not None:
            raise self.conn.execute_error
        self.conn.pending.append((sql, params))
        self.rowcount = self.conn.rowcount

    def fetchone(self) -> Any:
        return self.conn.rows.pop(0) if self.conn.rows else None

    def fetchall(self) -> list[Any]:
        return self.conn.rows.pop(0) if self.conn.rows else []


class RecordingConnection:
    def __init__(self, rows: list[Any] | None = None, *, rowcount: int = 0) -> None:
        self.rows = list(rows or [])
        self.rowcount = rowcount
        self.pending: list[tuple[str, tuple[object, ...] | None]] = []
        self.committed: list[tuple[str, tuple[object, ...] | None]] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.execute_error: Exception | None = None

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self)

    def commit(self) -> None:
        self.committed.extend(self.pending)
        self.pending.clear()
        self.commit_count += 1

    def rollback(self) -> None:
        self.pending.clear()
        self.rollback_count += 1

    def joined_sql(self) -> str:
        return "\n".join(sql for sql, _ in [*self.committed, *self.pending])

    def committed_sql(self) -> str:
        return "\n".join(sql for sql, _ in self.committed)


class SharedAlertBackend:
    def __init__(self) -> None:
        self.open = True
        self.locked = False
        self.notify_count = 0
        self.alert_id = 91

    def row(self) -> tuple[Any, ...]:
        return (self.alert_id, 7, 31, "failed", NOW - timedelta(hours=7), None, self.notify_count, {"status": "failed"})


class SharedAlertCursor:
    def __init__(self, conn: "SharedAlertConnection") -> None:
        self.conn = conn
        self.row: Any = None
        self.rowcount = 0

    def __enter__(self) -> "SharedAlertCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.conn.pending.append((sql, params))
        if "FROM sync_job_alerts" in sql and "WHERE id = %s AND state = 'open'" in sql:
            if "FOR UPDATE SKIP LOCKED" not in sql:
                raise AssertionError("alert lock query must use FOR UPDATE SKIP LOCKED")
            if self.conn.backend.open and not self.conn.backend.locked:
                self.conn.backend.locked = True
                self.conn.owns_lock = True
                self.row = self.conn.backend.row()
            else:
                self.row = None
        elif "INSERT INTO sync_job_alerts" in sql:
            if self.conn.backend.open:
                self.row = None
            else:
                self.conn.pending_open = True
                self.row = (92,)
        elif "SET state = 'resolved'" in sql:
            self.conn.pending_resolved = True
        elif "notify_count = notify_count + 1" in sql:
            self.conn.pending_notify = True

    def fetchone(self) -> Any:
        return self.row


class SharedAlertConnection:
    def __init__(self, backend: SharedAlertBackend) -> None:
        self.backend = backend
        self.pending: list[tuple[str, tuple[object, ...] | None]] = []
        self.committed: list[tuple[str, tuple[object, ...] | None]] = []
        self.owns_lock = False
        self.pending_open = False
        self.pending_resolved = False
        self.pending_notify = False

    def cursor(self) -> SharedAlertCursor:
        return SharedAlertCursor(self)

    def commit(self) -> None:
        if self.pending_resolved:
            self.backend.open = False
        if self.pending_open:
            self.backend.open = True
            self.backend.alert_id = 92
        if self.pending_notify:
            self.backend.notify_count += 1
        self.committed.extend(self.pending)
        self.pending.clear()
        self._release()

    def rollback(self) -> None:
        self.pending.clear()
        self._release()

    def _release(self) -> None:
        if self.owns_lock:
            self.backend.locked = False
            self.owns_lock = False


def make_unsigned_jwt(payload: dict[str, object]) -> str:
    def encode(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = encode({"alg": "none", "typ": "JWT"})
    return f"{header}.{encode(payload)}."


@contextmanager
def token_file(contents: str):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(contents)
        path = handle.name
    try:
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


class SyncAlertNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        self._clear_app_modules()
        sys.path[:] = [item for item in sys.path if item != str(WORKER_ROOT)]
        sys.path.insert(0, str(WORKER_ROOT))
        from app.pipelines import sync_alert_notifier

        self.notifier = sync_alert_notifier

    def tearDown(self) -> None:
        self._clear_app_modules()
        sys.path[:] = self._old_sys_path

    @staticmethod
    def _clear_app_modules() -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]

    def test_credential_threshold_and_recovery(self) -> None:
        exp = int((NOW + timedelta(days=4)).timestamp())
        with token_file(make_unsigned_jwt({"exp": exp})) as path:
            self.assertFalse(self.notifier.credential_status(path, now=NOW)["ok"])
        exp = int((NOW + timedelta(days=4, seconds=1)).timestamp())
        with token_file(make_unsigned_jwt({"exp": exp})) as path:
            self.assertTrue(self.notifier.credential_status(path, now=NOW)["ok"])

    def test_credential_status_handles_missing_empty_invalid_and_expired_tokens(self) -> None:
        missing = self.notifier.credential_status("not-a-real-token-path", now=NOW)
        self.assertEqual({"configured": False, "ok": False, "expired": False}, {
            key: missing[key] for key in ("configured", "ok", "expired")
        })
        self.assertIsNone(missing["expires_at"])
        self.assertIsNone(missing["remaining_hours"])

        cases = (
            ("", False),
            ("not.a.jwt", False),
            ("not.x.jwt", False),
            (make_unsigned_jwt({"exp": int((NOW - timedelta(seconds=1)).timestamp())}), True),
        )
        for token, expired in cases:
            with self.subTest(expired=expired), token_file(token) as path:
                status = self.notifier.credential_status(path, now=NOW)
                self.assertTrue(status["configured"])
                self.assertFalse(status["ok"])
                self.assertEqual(expired, status["expired"])
                if token:
                    self.assertNotIn(token, str(status))

    def test_credential_status_rejects_non_object_payload_and_non_finite_expiration(self) -> None:
        invalid_tokens = (
            make_unsigned_jwt([]),
            make_unsigned_jwt({"exp": float("inf")}),
        )
        for token in invalid_tokens:
            with self.subTest(token_kind=type(token).__name__), token_file(token) as path:
                status = self.notifier.credential_status(path, now=NOW)
                self.assertEqual(
                    {
                        "configured": True,
                        "ok": False,
                        "expired": False,
                        "expires_at": None,
                        "remaining_hours": None,
                        "message": "凭据格式无效",
                    },
                    status,
                )

    def test_artifact_requires_a_material_gap(self) -> None:
        started = NOW
        self.assertFalse(self.notifier.artifact_is_stale(started, [{"mtime_epoch": started.timestamp() - 300}]))
        self.assertTrue(self.notifier.artifact_is_stale(started, [{"mtime_epoch": started.timestamp() - 301}]))

    def test_artifact_is_stale_without_artifact_or_success_and_uses_latest_artifact(self) -> None:
        self.assertTrue(self.notifier.artifact_is_stale(NOW, []))
        self.assertTrue(self.notifier.artifact_is_stale(None, [{"mtime_epoch": NOW.timestamp()}]))
        artifacts = [
            {"mtime_epoch": NOW.timestamp() - 3600},
            {"mtime_epoch": NOW.timestamp() - 299},
            {"mtime_epoch": None},
        ]
        self.assertFalse(self.notifier.artifact_is_stale(NOW, artifacts))

    def test_error_kind_labels_are_safe_chinese_phrases(self) -> None:
        expected = {
            "auth": "凭据过期",
            "rate_limit": "请求限流",
            "network": "网络异常",
            "schema": "数据结构变化",
            "write": "写入失败",
            "unknown": "未知错误",
        }
        self.assertEqual(expected, {kind: self.notifier.error_kind_label(kind) for kind in expected})
        self.assertEqual("未知错误", self.notifier.error_kind_label(None))
        self.assertEqual("未知错误", self.notifier.error_kind_label("unrecognized"))

    def test_failed_message_is_classified_and_links_to_job(self) -> None:
        text = self.notifier.build_alert_text("open", {
            "alert_kind": "failed", "job_key": "wecom.doc.17", "display_name": "企微·点检表",
            "error_kind": "auth", "last_success_at": NOW - timedelta(hours=32),
            "consecutive_failures": 3,
        }, now=NOW)
        self.assertIn("同步失败：企微·点检表", text)
        self.assertIn("凭据过期(auth)", text)
        self.assertIn("连续失败 3 次", text)
        self.assertIn("https://hydwang.xyz/sync/?job=wecom.doc.17", text)

    def test_unknown_error_kind_is_normalized_before_rendering(self) -> None:
        text = self.notifier.build_alert_text("open", {
            "alert_kind": "failed",
            "job_key": "wecom.doc.17",
            "display_name": "企微·点检表",
            "error_kind": "traceback: synthetic-secret",
        }, now=NOW)

        self.assertIn("未知错误(unknown)", text)
        self.assertNotIn("traceback: synthetic-secret", text)

    def test_alert_titles_and_messages_do_not_leak_external_values(self) -> None:
        alert = {
            "alert_kind": "failed",
            "job_key": "wecom.doc/17 ?",
            "display_name": "企微·点检表",
            "error_kind": "network",
            "last_success_at": NOW - timedelta(hours=1),
            "consecutive_failures": 1,
            "external_doc_id": "synthetic-doc-id",
            "token": "synthetic-token",
            "secret": "synthetic-secret",
            "traceback": "synthetic-traceback",
        }
        expected_titles = {
            "open": "同步告警",
            "escalate": "同步告警升级",
            "resolved": "同步已恢复",
        }
        for event, title in expected_titles.items():
            with self.subTest(event=event):
                text = self.notifier.build_alert_text(event, alert, now=NOW)
                self.assertIn(title, text)
                self.assertIn("https://hydwang.xyz/sync/?job=wecom.doc%2F17%20%3F", text)
                for value in ("synthetic-doc-id", "synthetic-token", "synthetic-secret", "synthetic-traceback"):
                    self.assertNotIn(value, text)


class SyncAlertRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        SyncAlertNotifierTests._clear_app_modules()
        sys.path[:] = [item for item in sys.path if item != str(WORKER_ROOT)]
        sys.path.insert(0, str(WORKER_ROOT))
        from app.pipelines import sync_alert_notifier

        self.notifier = sync_alert_notifier
        self.conn = RecordingConnection()
        self.repo = self.notifier.SyncAlertRepository(self.conn, now_fn=lambda: NOW)
        self.job = {"id": 7, "job_key": "wecom.doc.17"}

    def tearDown(self) -> None:
        SyncAlertNotifierTests._clear_app_modules()
        sys.path[:] = self._old_sys_path

    @staticmethod
    def _open_alert(last_notified_at: datetime | None = None) -> tuple[Any, ...]:
        return (91, 7, 31, "failed", NOW - timedelta(hours=7), last_notified_at, 0, {"status": "failed"})

    def test_claim_uses_exact_partial_index_inference(self) -> None:
        self.conn.rows = [(91,)]

        alert_id = self.repo.claim_alert(self.job, 31, "failed", {"status": "failed"})

        sql = self.conn.joined_sql()
        self.assertIn("ON CONFLICT (job_id, alert_kind) WHERE state = 'open' DO NOTHING", sql)
        self.assertIn("RETURNING id", sql)
        self.assertNotIn("ON CONFLICT DO NOTHING", sql)
        self.assertEqual(91, alert_id)

    def test_chanjet_defaults_only_fill_null_operator_fields(self) -> None:
        self.repo.ensure_chanjet_defaults()

        sql = self.conn.joined_sql()
        self.assertIn("freshness_sla_seconds = COALESCE(freshness_sla_seconds, %s)", sql)
        self.assertIn("artifact_glob = COALESCE(artifact_glob, %s)", sql)
        self.assertEqual(
            (172800, "/app/tplus-output/excel/*.xlsx", "chanjet.full"),
            self.conn.committed[-1][1],
        )

    def test_claim_uses_jsonb_payload_and_returns_none_when_already_open(self) -> None:
        self.conn.rows = [None]

        self.assertIsNone(self.repo.claim_alert(self.job, 31, "failed", {"status": "failed"}))

        params = self.conn.committed[0][1]
        self.assertEqual((7, 31, "failed"), params[:3])
        self.assertNotIsInstance(params[3], dict)
        self.assertEqual({"status": "failed"}, getattr(params[3], "obj", getattr(params[3], "value", None)))

    def test_delivery_holds_row_lock_and_only_marks_successful_send(self) -> None:
        self.conn.rows = [self._open_alert()]

        self.assertTrue(self.repo.deliver_due(91, lambda alert: alert["id"] == 91))

        self.assertIn("FOR UPDATE SKIP LOCKED", self.conn.joined_sql())
        self.assertIn("notify_count = notify_count + 1", self.conn.joined_sql())
        self.assertEqual(1, self.conn.commit_count)

    def test_claim_loser_does_not_push_and_lock_loser_does_not_send(self) -> None:
        self.conn.rows = [None]
        self.assertIsNone(self.repo.claim_alert(self.job, 31, "failed", {"status": "failed"}))

        locked_elsewhere = RecordingConnection([None])
        other_repo = self.notifier.SyncAlertRepository(locked_elsewhere, now_fn=lambda: NOW)
        self.assertFalse(other_repo.deliver_due(91, lambda _: self.fail("sender must not run")))
        self.assertEqual(0, locked_elsewhere.commit_count)

    def test_delivery_is_not_due_at_exact_six_hour_boundary(self) -> None:
        self.conn.rows = [self._open_alert(NOW - timedelta(hours=6))]

        self.assertFalse(self.repo.deliver_due(91, lambda _: self.fail("sender must not run")))

        self.assertNotIn("notify_count = notify_count + 1", self.conn.committed_sql())

    def test_delivery_escalates_after_six_hours_with_parameterized_cutoff(self) -> None:
        self.conn.rows = [self._open_alert(NOW - timedelta(hours=6, microseconds=1))]

        self.assertTrue(self.repo.deliver_due(91, lambda _: True))

        lock_params = self.conn.committed[0][1]
        self.assertEqual((91,), lock_params)
        self.assertIn(
            NOW - timedelta(seconds=21600),
            [param for _, params in self.conn.committed for param in (params or ())],
        )

    def test_delivery_sends_and_persists_current_payload_under_the_row_lock(self) -> None:
        self.conn.rows = [self._open_alert(NOW - timedelta(hours=7))]
        current_payload = {
            "status": "failed",
            "error_kind": "auth",
            "consecutive_failures": 4,
        }
        delivered: list[dict[str, Any]] = []

        self.assertTrue(self.repo.deliver_due(
            91, lambda alert: delivered.append(alert) is None, payload=current_payload
        ))

        self.assertEqual("auth", delivered[0]["error_kind"])
        self.assertEqual(4, delivered[0]["consecutive_failures"])
        update_params = self.conn.committed[-1][1]
        stored = getattr(update_params[0], "obj", getattr(update_params[0], "value", None))
        self.assertEqual(current_payload, stored)
        self.assertIn("payload_json = %s", self.conn.committed[-1][0])

    def test_failed_delivery_does_not_persist_current_payload(self) -> None:
        self.conn.rows = [self._open_alert(NOW - timedelta(hours=7))]

        self.assertFalse(self.repo.deliver_due(
            91, lambda _: False, payload={"error_kind": "auth"}
        ))

        self.assertEqual(1, self.conn.rollback_count)
        self.assertNotIn("payload_json = %s", self.conn.committed_sql())

    def test_second_connection_skips_locked_alert_until_first_sender_returns(self) -> None:
        backend = SharedAlertBackend()
        first_conn = SharedAlertConnection(backend)
        second_conn = SharedAlertConnection(backend)
        first_repo = self.notifier.SyncAlertRepository(first_conn, now_fn=lambda: NOW)
        second_repo = self.notifier.SyncAlertRepository(second_conn, now_fn=lambda: NOW)
        first_sends: list[int] = []
        second_sends: list[int] = []

        def first_sender(alert: dict[str, Any]) -> bool:
            first_sends.append(alert["id"])
            self.assertFalse(second_repo.deliver_due(91, lambda item: second_sends.append(item["id"]) is None))
            self.assertTrue(backend.locked)
            return True

        self.assertTrue(first_repo.deliver_due(91, first_sender))
        self.assertEqual([91], first_sends)
        self.assertEqual([], second_sends)
        self.assertEqual(1, backend.notify_count)

    def test_failed_delivery_rolls_back_for_next_poll(self) -> None:
        self.conn.rows = [self._open_alert()]

        self.assertFalse(self.repo.deliver_due(91, lambda _: False))

        self.assertEqual(1, self.conn.rollback_count)
        self.assertNotIn("notify_count = notify_count + 1", self.conn.committed_sql())

    def test_resolve_writes_state_only_after_successful_recovery_send(self) -> None:
        self.conn.rows = [self._open_alert()]

        self.assertTrue(self.repo.resolve_alert(91, {"status": "success"}, lambda alert: alert["status"] == "success"))

        sql = self.conn.committed_sql()
        self.assertIn("state = 'resolved'", sql)
        self.assertIn("resolved_at = NOW()", sql)
        params = self.conn.committed[-1][1]
        self.assertEqual(91, params[-1])
        self.assertNotIsInstance(params[0], dict)

    def test_failed_recovery_rolls_back_and_leaves_alert_open_for_retry(self) -> None:
        self.conn.rows = [self._open_alert()]

        self.assertFalse(self.repo.resolve_alert(91, {"status": "success"}, lambda _: False))

        self.assertEqual(1, self.conn.rollback_count)
        self.assertNotIn("state = 'resolved'", self.conn.committed_sql())

    def test_resolved_alert_releases_shared_open_unique_constraint_for_new_claim(self) -> None:
        backend = SharedAlertBackend()
        conn = SharedAlertConnection(backend)
        repo = self.notifier.SyncAlertRepository(conn, now_fn=lambda: NOW)

        self.assertTrue(repo.resolve_alert(91, {"status": "success"}, lambda _: True))
        self.assertEqual(92, repo.claim_alert(self.job, 32, "failed", {"status": "failed"}))
        self.assertTrue(backend.open)

    def test_load_job_states_returns_task3_ready_nested_shape_and_commits_read_transaction(self) -> None:
        latest_run_detail = {"phase": "fetch"}
        latest_success_detail = {"rows": 8}
        open_alerts = [{
            "id": 91,
            "run_id": 31,
            "alert_kind": "failed",
            "first_seen_at": NOW - timedelta(hours=7),
            "last_notified_at": NOW - timedelta(hours=1),
            "notify_count": 2,
            "payload_json": {"status": "failed"},
        }]
        self.conn.rows = [[(
            7, "wecom.doc.17", "wecom", "点检表", True, True, "oc_alerts", 3600, "exports/*.json",
            31, "failed", NOW - timedelta(minutes=5), NOW - timedelta(minutes=4), "network", "timeout", latest_run_detail,
            29, NOW - timedelta(hours=2), NOW - timedelta(hours=1), latest_success_detail,
            3, open_alerts,
        )]]

        states = self.repo.load_job_states()

        self.assertEqual([{
            "job": {
                "id": 7,
                "job_key": "wecom.doc.17",
                "provider": "wecom",
                "display_name": "点检表",
                "enabled": True,
                "alert_enabled": True,
                "alert_chat_id": "oc_alerts",
                "freshness_sla_seconds": 3600,
                "artifact_glob": "exports/*.json",
            },
            "latest_run": {
                "id": 31,
                "status": "failed",
                "started_at": NOW - timedelta(minutes=5),
                "finished_at": NOW - timedelta(minutes=4),
                "error_kind": "network",
                "error_message": "timeout",
                "detail_json": latest_run_detail,
            },
            "latest_success": {
                "id": 29,
                "started_at": NOW - timedelta(hours=2),
                "finished_at": NOW - timedelta(hours=1),
                "detail_json": latest_success_detail,
            },
            "consecutive_failures": 3,
            "open_alerts": open_alerts,
        }], states)
        self.assertEqual(1, self.conn.commit_count)
        sql = self.conn.joined_sql()
        self.assertIn("sync_jobs", sql)
        self.assertIn("sync_job_runs", sql)
        self.assertIn("sync_job_alerts", sql)
        self.assertNotIn("external_sources", sql)
        self.assertNotIn("external_doc_id", sql)

    def test_load_job_states_rolls_back_and_reraises_query_errors(self) -> None:
        self.conn.execute_error = RuntimeError("synthetic read failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic read failure"):
            self.repo.load_job_states()

        self.assertEqual(1, self.conn.rollback_count)

    def test_nondefault_escalation_boundary_is_exclusive(self) -> None:
        repo = self.notifier.SyncAlertRepository(self.conn, now_fn=lambda: NOW, escalation_seconds=12)
        self.conn.rows = [self._open_alert(NOW - timedelta(seconds=12))]

        self.assertFalse(repo.deliver_due(91, lambda _: self.fail("sender must not run at cutoff")))

        self.conn.rows = [self._open_alert(NOW - timedelta(seconds=12, microseconds=1))]
        self.assertTrue(repo.deliver_due(91, lambda _: True))

    def test_cleanup_uses_30_and_90_day_windows(self) -> None:
        self.conn.rowcount = 4

        self.assertEqual(4, self.repo.cleanup_steps())

        sql = self.conn.joined_sql()
        self.assertIn("r.status = 'success'", sql)
        self.assertIn("INTERVAL '30 days'", sql)
        self.assertIn("r.status <> 'success'", sql)
        self.assertIn("INTERVAL '90 days'", sql)


class InMemoryAlertRepository:
    def __init__(self, states: list[dict[str, Any]], *, now_fn) -> None:
        self.states = states
        self.now_fn = now_fn
        self.escalation_seconds = 21600
        self.alerts: dict[int, dict[str, Any]] = {}
        self.next_id = 1
        self.cleanup_calls = 0
        self.events: list[str] = []

    def ensure_chanjet_defaults(self) -> None:
        self.events.append("defaults")

    def load_job_states(self) -> list[dict[str, Any]]:
        self.events.append("load")
        result = []
        for state in self.states:
            item = dict(state)
            item["open_alerts"] = [
                dict(alert) for alert in self.alerts.values()
                if alert["job_id"] == state["job"]["id"] and alert["state"] == "open"
            ]
            result.append(item)
        return result

    def claim_alert(
        self, job: dict[str, Any], run_id: int | None, alert_kind: str, payload: dict[str, Any]
    ) -> int | None:
        self.events.append(f"claim:{job['id']}:{alert_kind}")
        if any(
            alert["job_id"] == job["id"] and alert["alert_kind"] == alert_kind and alert["state"] == "open"
            for alert in self.alerts.values()
        ):
            return None
        alert_id = self.next_id
        self.next_id += 1
        self.alerts[alert_id] = {
            "id": alert_id,
            "job_id": job["id"],
            "run_id": run_id,
            "alert_kind": alert_kind,
            "first_seen_at": self.now_fn(),
            "last_notified_at": None,
            "notify_count": 0,
            "payload_json": dict(payload),
            "state": "open",
        }
        return alert_id

    def deliver_due(self, alert_id: int, sender, payload: dict[str, Any] | None = None) -> bool:
        self.events.append(f"deliver:{alert_id}")
        alert = self.alerts[alert_id]
        if alert["state"] != "open":
            return False
        cutoff = self.now_fn() - timedelta(seconds=self.escalation_seconds)
        if alert["last_notified_at"] is not None and alert["last_notified_at"] >= cutoff:
            return False
        delivered = dict(alert)
        delivered.update(alert["payload_json"])
        if payload is not None:
            delivered.update(payload)
        if not sender(delivered):
            return False
        alert["last_notified_at"] = self.now_fn()
        alert["notify_count"] += 1
        if payload is not None:
            alert["payload_json"] = dict(payload)
        return True

    def resolve_alert(self, alert_id: int, payload: dict[str, Any], sender) -> bool:
        alert = self.alerts[alert_id]
        self.events.append(f"resolve:{alert['job_id']}:{alert['alert_kind']}")
        delivered = dict(alert)
        delivered.update(alert["payload_json"])
        delivered.update(payload)
        if alert["state"] != "open" or not sender(delivered):
            return False
        alert["state"] = "resolved"
        alert["payload_json"] = dict(payload)
        return True

    def cleanup_steps(self) -> int:
        self.events.append("cleanup")
        self.cleanup_calls += 1
        return 3


class SyncAlertOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        self._old_env = os.environ.copy()
        SyncAlertNotifierTests._clear_app_modules()
        sys.path[:] = [item for item in sys.path if item != str(WORKER_ROOT)]
        sys.path.insert(0, str(WORKER_ROOT))
        from app.pipelines import sync_alert_notifier

        self.notifier = sync_alert_notifier
        self.now = NOW
        self.sent: list[tuple[str, str]] = []
        self.send_ok = True
        self.job = {
            "id": 7,
            "job_key": "wecom.doc.17",
            "provider": "wecom",
            "display_name": "点检表",
            "enabled": True,
            "alert_enabled": True,
            "alert_chat_id": "oc_job",
            "freshness_sla_seconds": None,
            "artifact_glob": None,
        }
        self.repository = InMemoryAlertRepository([], now_fn=lambda: self.now)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        SyncAlertNotifierTests._clear_app_modules()
        sys.path[:] = self._old_sys_path

    def sender(self, chat_id: str, text: str) -> bool:
        self.sent.append((chat_id, text))
        return self.send_ok

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)

    def run_with_latest(
        self,
        *,
        status: str,
        run_id: int,
        error_kind: str | None = None,
        error_message: str | None = None,
        latest_success_at: datetime | None = None,
        latest_success_id: int | None = None,
        latest_success_detail: dict[str, Any] | None = None,
        consecutive_failures: int | None = None,
    ) -> dict[str, int]:
        if latest_success_at is None and status == "success":
            latest_success_at = self.now
        latest_success = None
        if latest_success_at is not None:
            latest_success = {
                "id": latest_success_id if latest_success_id is not None else (
                    run_id if status == "success" else max(1, run_id - 1)
                ),
                "started_at": latest_success_at,
                "finished_at": latest_success_at,
                "detail_json": latest_success_detail or {},
            }
        self.repository.states = [{
            "job": dict(self.job),
            "latest_run": {
                "id": run_id,
                "status": status,
                "started_at": self.now,
                "finished_at": self.now,
                "error_kind": error_kind,
                "error_message": error_message,
                "detail_json": {},
            },
            "latest_success": latest_success,
            "consecutive_failures": consecutive_failures if consecutive_failures is not None else (
                1 if status in {"failed", "partial"} else 0
            ),
            "open_alerts": [],
        }]
        return self.notifier.run_notifier_once(
            repository=self.repository, sender=self.sender, now=self.now
        )

    @staticmethod
    def pick(result: dict[str, int], *keys: str) -> dict[str, int]:
        return {key: result[key] for key in keys}

    def test_failed_open_escalate_recover_and_reopen(self) -> None:
        first = self.run_with_latest(
            status="failed", run_id=10, error_kind="network",
            error_message="timeout access_token=synthetic-secret",
            latest_success_at=self.now - timedelta(hours=1),
        )
        self.assertEqual({"opened": 1, "notified": 1}, self.pick(first, "opened", "notified"))
        payload = self.repository.alerts[1]["payload_json"]
        self.assertEqual("network", payload["error_kind"])
        self.assertEqual(1, payload["consecutive_failures"])
        self.assertNotIn("synthetic-secret", repr(payload))
        json.dumps(payload)

        self.advance(hours=6)
        self.assertEqual(0, self.run_with_latest(status="failed", run_id=10)["escalated"])
        self.advance(seconds=1)
        self.assertEqual(1, self.run_with_latest(status="failed", run_id=10)["escalated"])
        self.assertEqual(1, self.run_with_latest(status="success", run_id=11)["resolved"])
        self.assertEqual(1, self.run_with_latest(status="partial", run_id=12)["opened"])

    def test_recovered_alert_past_escalation_threshold_only_resolves(self) -> None:
        self.run_with_latest(status="failed", run_id=10, error_kind="network")
        self.advance(hours=6, seconds=1)

        result = self.run_with_latest(status="success", run_id=11)

        self.assertEqual({"resolved": 1, "escalated": 0, "notified": 0}, self.pick(
            result, "resolved", "escalated", "notified"
        ))
        self.assertIn("同步已恢复", self.sent[-1][1])

    def test_failed_alert_stays_open_through_running_and_old_success_until_new_success(self) -> None:
        self.run_with_latest(status="failed", run_id=10, error_kind="network")
        initial_send_count = len(self.sent)

        running = self.run_with_latest(status="running", run_id=11)
        self.assertEqual(0, running["resolved"])
        self.assertEqual(initial_send_count, len(self.sent))
        self.assertEqual("open", self.repository.alerts[1]["state"])

        old_success = self.run_with_latest(
            status="running", run_id=11,
            latest_success_at=self.now - timedelta(hours=1), latest_success_id=9,
        )
        self.assertEqual(0, old_success["resolved"])
        self.assertEqual(initial_send_count, len(self.sent))
        self.assertEqual("open", self.repository.alerts[1]["state"])

        new_success = self.run_with_latest(status="success", run_id=12)
        self.assertEqual(1, new_success["resolved"])
        self.assertEqual("resolved", self.repository.alerts[1]["state"])

    def test_escalation_uses_latest_error_and_failure_count(self) -> None:
        self.run_with_latest(
            status="failed", run_id=10, error_kind="network", consecutive_failures=1
        )
        self.advance(hours=6, seconds=1)

        result = self.run_with_latest(
            status="failed", run_id=11, error_kind="auth", consecutive_failures=4
        )

        self.assertEqual(1, result["escalated"])
        self.assertIn("凭据过期(auth)", self.sent[-1][1])
        self.assertIn("连续失败 4 次", self.sent[-1][1])
        self.assertEqual("auth", self.repository.alerts[1]["payload_json"]["error_kind"])
        self.assertEqual(4, self.repository.alerts[1]["payload_json"]["consecutive_failures"])

    def test_failed_error_payload_redacts_secrets_paths_and_traceback(self) -> None:
        error = (
            "token=raw-token secret=raw-secret external_doc_id=raw-doc "
            "Authorization: Bearer raw-auth C:\\private\\dump.txt /srv/private/dump.txt\n"
            "Traceback (most recent call last): raw-trace"
        )

        self.run_with_latest(
            status="failed", run_id=10, error_kind="unknown", error_message=error
        )

        payload = repr(self.repository.alerts[1]["payload_json"])
        for forbidden in (
            "raw-token", "raw-secret", "raw-doc", "raw-auth",
            "C:\\private\\dump.txt", "/srv/private/dump.txt", "Traceback", "raw-trace",
        ):
            self.assertNotIn(forbidden, payload)

    def test_stale_opens_then_resolves_inside_sla(self) -> None:
        self.job["freshness_sla_seconds"] = 3600
        result = self.run_with_latest(
            status="success", run_id=10, latest_success_at=self.now - timedelta(hours=2)
        )
        self.assertEqual(1, result["opened"])
        self.assertEqual("stale", self.repository.alerts[1]["alert_kind"])

        result = self.run_with_latest(
            status="success", run_id=11, latest_success_at=self.now - timedelta(minutes=30)
        )
        self.assertEqual(1, result["resolved"])

    def test_artifact_snapshot_and_live_glob_open_then_resolve_without_path_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "export.xlsx"
            artifact.write_text("old", encoding="utf-8")
            old_epoch = (self.now - timedelta(hours=2)).timestamp()
            os.utime(artifact, (old_epoch, old_epoch))
            self.job["artifact_glob"] = str(Path(tmp) / "*.xlsx")
            snapshot = [{"name": str(artifact), "mtime_epoch": old_epoch}]

            result = self.run_with_latest(
                status="success", run_id=10, latest_success_at=self.now,
                latest_success_detail={"artifacts": snapshot},
            )
            self.assertEqual(1, result["opened"])
            self.assertEqual("artifact_stale", self.repository.alerts[1]["alert_kind"])
            self.assertNotIn(tmp, repr(self.repository.alerts[1]["payload_json"]))

            fresh_epoch = self.now.timestamp()
            os.utime(artifact, (fresh_epoch, fresh_epoch))
            result = self.run_with_latest(
                status="success", run_id=11, latest_success_at=self.now,
                latest_success_detail={"artifacts": snapshot},
            )
            self.assertEqual(1, result["resolved"])

    def test_chanjet_token_expiring_opens_and_refresh_resolves_without_token_or_path(self) -> None:
        self.job["job_key"] = "chanjet.full"
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "open-token.txt"
            os.environ["CHANJET_OPEN_TOKEN_FILE"] = str(token_path)
            token_path.write_text(
                make_unsigned_jwt({"exp": int((self.now + timedelta(days=3)).timestamp())}),
                encoding="utf-8",
            )
            result = self.run_with_latest(status="success", run_id=10)
            self.assertEqual(1, result["opened"])
            self.assertEqual("credential_expiring", self.repository.alerts[1]["alert_kind"])
            payload_text = repr(self.repository.alerts[1]["payload_json"])
            self.assertNotIn(str(token_path), payload_text)
            self.assertNotIn(token_path.read_text(encoding="utf-8"), payload_text)
            json.dumps(self.repository.alerts[1]["payload_json"])

            token_path.write_text(
                make_unsigned_jwt({"exp": int((self.now + timedelta(days=5)).timestamp())}),
                encoding="utf-8",
            )
            self.assertEqual(1, self.run_with_latest(status="success", run_id=11)["resolved"])

    def test_alert_disabled_skips_all_conditions_but_still_cleans_up(self) -> None:
        self.job["alert_enabled"] = False
        result = self.run_with_latest(status="failed", run_id=10, error_kind="network")
        self.assertEqual(0, result["opened"])
        self.assertEqual({}, self.repository.alerts)
        self.assertEqual(1, self.repository.cleanup_calls)
        self.assertEqual(3, result["cleaned"])

    def test_job_chat_overrides_global_and_failed_send_retries_next_poll(self) -> None:
        os.environ["SYNC_ALERT_CHAT_ID"] = "oc_global"
        self.send_ok = False
        first = self.run_with_latest(status="failed", run_id=10, error_kind="network")
        self.assertEqual({"opened": 1, "notified": 0}, self.pick(first, "opened", "notified"))
        self.assertEqual("oc_job", self.sent[-1][0])

        self.send_ok = True
        second = self.run_with_latest(status="failed", run_id=10, error_kind="network")
        self.assertEqual(1, second["notified"])
        self.assertEqual(2, len(self.sent))

    def test_missing_chat_records_open_without_calling_sender(self) -> None:
        self.job["alert_chat_id"] = ""
        os.environ.pop("SYNC_ALERT_CHAT_ID", None)
        result = self.run_with_latest(status="partial", run_id=10, error_kind="schema")
        self.assertEqual({"opened": 1, "notified": 0}, self.pick(result, "opened", "notified"))
        self.assertEqual([], self.sent)

    def test_missing_feishu_credentials_records_open_for_later_retry(self) -> None:
        state = {
            "job": dict(self.job),
            "latest_run": {
                "id": 10, "status": "failed", "started_at": self.now,
                "finished_at": self.now, "error_kind": "network",
                "error_message": "timeout", "detail_json": {},
            },
            "latest_success": None,
            "consecutive_failures": 1,
            "open_alerts": [],
        }
        self.repository.states = [state]
        with mock.patch.object(self.notifier, "credentials_for_profile", return_value=[]):
            result = self.notifier.run_notifier_once(repository=self.repository, now=self.now)
        self.assertEqual({"opened": 1, "notified": 0}, self.pick(result, "opened", "notified"))
        self.assertEqual("open", self.repository.alerts[1]["state"])

    def test_failed_and_stale_can_coexist_without_parent_match_business_message(self) -> None:
        self.job["freshness_sla_seconds"] = 60
        from app.pipelines import tplus_parent_match

        with mock.patch.object(tplus_parent_match, "send_feishu_alert") as business_sender:
            result = self.run_with_latest(
                status="failed", run_id=10, error_kind="network",
                latest_success_at=self.now - timedelta(hours=2),
            )
        self.assertEqual(2, result["opened"])
        self.assertEqual(2, result["notified"])
        self.assertEqual({"failed", "stale"}, {
            alert["alert_kind"] for alert in self.repository.alerts.values()
        })
        run_ids = {
            alert["alert_kind"]: alert["run_id"] for alert in self.repository.alerts.values()
        }
        self.assertEqual({"failed": 10, "stale": None}, run_ids)
        business_sender.assert_not_called()

    def test_custom_positive_intervals_and_fallbacks_are_applied(self) -> None:
        os.environ["SYNC_ALERT_ESCALATION_SECONDS"] = "12"
        os.environ["SYNC_ARTIFACT_GRACE_SECONDS"] = "7"
        with mock.patch.object(self.notifier, "artifact_is_stale", return_value=False) as predicate:
            self.job["artifact_glob"] = "missing/*.xlsx"
            self.run_with_latest(status="success", run_id=10, latest_success_at=self.now)
        self.assertEqual(12, self.repository.escalation_seconds)
        self.assertEqual(7, predicate.call_args.kwargs["grace_seconds"])

        os.environ["SYNC_ALERT_ESCALATION_SECONDS"] = "0"
        os.environ["SYNC_ARTIFACT_GRACE_SECONDS"] = "invalid"
        with mock.patch.object(self.notifier, "artifact_is_stale", return_value=False) as predicate:
            self.run_with_latest(status="success", run_id=11, latest_success_at=self.now)
        self.assertEqual(21600, self.repository.escalation_seconds)
        self.assertEqual(300, predicate.call_args.kwargs["grace_seconds"])

    def test_all_jobs_resolve_before_any_claim_and_all_claim_before_delivery(self) -> None:
        first_job = dict(self.job)
        second_job = {**self.job, "id": 8, "job_key": "wecom.doc.18", "display_name": "台账"}
        self.repository.claim_alert(first_job, None, "stale", {
            "job_key": first_job["job_key"], "display_name": first_job["display_name"]
        })
        self.repository.claim_alert(second_job, 20, "failed", {
            "job_key": second_job["job_key"], "display_name": second_job["display_name"]
        })
        self.repository.states = [
            {
                "job": first_job,
                "latest_run": {
                    "id": 11, "status": "failed", "started_at": self.now,
                    "finished_at": self.now, "error_kind": "network",
                    "error_message": "timeout", "detail_json": {},
                },
                "latest_success": {
                    "id": 10, "started_at": self.now, "finished_at": self.now,
                    "detail_json": {},
                },
                "consecutive_failures": 1,
                "open_alerts": [],
            },
            {
                "job": second_job,
                "latest_run": {
                    "id": 21, "status": "success", "started_at": self.now,
                    "finished_at": self.now, "error_kind": None,
                    "error_message": None, "detail_json": {},
                },
                "latest_success": {
                    "id": 21, "started_at": self.now, "finished_at": self.now,
                    "detail_json": {},
                },
                "consecutive_failures": 0,
                "open_alerts": [],
            },
        ]
        self.repository.events.clear()

        self.notifier.run_notifier_once(
            repository=self.repository, sender=self.sender, now=self.now
        )

        phases = [event.split(":", 1)[0] for event in self.repository.events]
        resolve_indexes = [index for index, phase in enumerate(phases) if phase == "resolve"]
        claim_indexes = [index for index, phase in enumerate(phases) if phase == "claim"]
        deliver_indexes = [index for index, phase in enumerate(phases) if phase == "deliver"]
        self.assertLess(max(resolve_indexes), min(claim_indexes))
        self.assertLess(max(claim_indexes), min(deliver_indexes))
        self.assertEqual("cleanup", phases[-1])

    def test_notifier_applies_chanjet_defaults_before_loading_jobs(self) -> None:
        self.run_with_latest(status="success", run_id=10)

        self.assertLess(self.repository.events.index("defaults"), self.repository.events.index("load"))

    def test_notifier_continues_loading_jobs_when_chanjet_defaults_write_fails(self) -> None:
        with mock.patch.object(
            self.repository, "ensure_chanjet_defaults", side_effect=RuntimeError("database unavailable")
        ):
            result = self.run_with_latest(status="success", run_id=10)

        self.assertEqual(1, result["checked"])
        self.assertIn("load", self.repository.events)


class FeishuAlertSenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        SyncAlertNotifierTests._clear_app_modules()
        sys.path[:] = [item for item in sys.path if item != str(WORKER_ROOT)]
        sys.path.insert(0, str(WORKER_ROOT))
        from app.pipelines import sync_alert_notifier

        self.notifier = sync_alert_notifier

    def tearDown(self) -> None:
        SyncAlertNotifierTests._clear_app_modules()
        sys.path[:] = self._old_sys_path

    def test_missing_credentials_returns_false_without_request(self) -> None:
        with mock.patch.object(self.notifier, "credentials_for_profile", return_value=[]), mock.patch.object(
            self.notifier, "FeishuBitableClient"
        ) as client:
            self.assertFalse(self.notifier.send_feishu_text("oc_alert", "hello"))
        client.assert_not_called()

    def test_sender_posts_feishu_text_with_configured_profile(self) -> None:
        credential = mock.Mock(app_id="app", app_secret="secret", api_base="https://feishu.invalid")
        client = mock.Mock()
        client._headers.return_value = {"Authorization": "Bearer tenant"}
        with mock.patch.dict(os.environ, {"SYNC_ALERT_FEISHU_PROFILE": "OPS"}, clear=False), mock.patch.object(
            self.notifier, "credentials_for_profile", return_value=[credential]
        ) as load_credentials, mock.patch.object(
            self.notifier, "FeishuBitableClient", return_value=client
        ):
            self.assertTrue(self.notifier.send_feishu_text("oc_alert", "同步异常"))

        load_credentials.assert_called_once_with("OPS")
        client._request_json.assert_called_once_with(
            "POST", "/im/v1/messages",
            headers={"Authorization": "Bearer tenant"},
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": "oc_alert",
                "msg_type": "text",
                "content": json.dumps({"text": "同步异常"}, ensure_ascii=False),
            },
        )
        client.session.close.assert_called_once_with()

    def test_sender_exception_log_contains_only_exception_type(self) -> None:
        credential = mock.Mock(app_id="app", app_secret="secret", api_base="https://feishu.invalid")
        client = mock.Mock()
        client._headers.side_effect = RuntimeError("response synthetic-secret oc_sensitive")
        output = io.StringIO()
        with mock.patch.object(
            self.notifier, "credentials_for_profile", return_value=[credential]
        ), mock.patch.object(
            self.notifier, "FeishuBitableClient", return_value=client
        ), mock.patch("sys.stdout", output):
            self.assertFalse(self.notifier.send_feishu_text("oc_sensitive", "hello"))
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertNotIn("oc_sensitive", output.getvalue())
        client.session.close.assert_called_once_with()

    def test_session_close_exception_does_not_escape_or_change_success(self) -> None:
        credential = mock.Mock(app_id="app", app_secret="secret", api_base="https://feishu.invalid")
        client = mock.Mock()
        client._headers.return_value = {"Authorization": "Bearer tenant"}
        client.session.close.side_effect = RuntimeError("close failed")
        with mock.patch.object(
            self.notifier, "credentials_for_profile", return_value=[credential]
        ), mock.patch.object(
            self.notifier, "FeishuBitableClient", return_value=client
        ):
            self.assertTrue(self.notifier.send_feishu_text("oc_alert", "hello"))
        client.session.close.assert_called_once_with()

    def test_credential_loader_exception_is_safely_reported(self) -> None:
        output = io.StringIO()
        with mock.patch.object(
            self.notifier, "credentials_for_profile",
            side_effect=RuntimeError("synthetic-secret oc_sensitive"),
        ), mock.patch("sys.stdout", output):
            self.assertFalse(self.notifier.send_feishu_text("oc_sensitive", "hello"))
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertNotIn("oc_sensitive", output.getvalue())


if __name__ == "__main__":
    unittest.main()
