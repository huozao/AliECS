from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


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


if __name__ == "__main__":
    unittest.main()
