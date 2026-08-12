from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


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


if __name__ == "__main__":
    unittest.main()
