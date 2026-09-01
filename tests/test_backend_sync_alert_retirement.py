from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "services" / "backend-api" / "app" / "routers" / "ops.py"
WORKER_ROOT = ROOT / "services" / "doc-sync-worker"


def _clear_worker_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _synthetic_jwt(expires_at: datetime) -> str:
    def encode(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode({'exp': int(expires_at.timestamp())})}."


class _CredentialAlertRepository:
    def __init__(self, state: dict[str, object], now: datetime) -> None:
        self.state = state
        self.now = now
        self.escalation_seconds = 21600
        self.claimed: list[dict[str, object]] = []
        self.delivered: list[dict[str, object]] = []

    def ensure_chanjet_defaults(self) -> None:
        return None

    def load_job_states(self) -> list[dict[str, object]]:
        return [self.state]

    def resolve_alert(self, *_args: object, **_kwargs: object) -> bool:
        return False

    def claim_alert(
        self, job: dict[str, object], run_id: int | None, alert_kind: str, payload: dict[str, object]
    ) -> int:
        self.claimed.append({
            "id": 1,
            "job_id": job["id"],
            "run_id": run_id,
            "alert_kind": alert_kind,
            "payload_json": dict(payload),
        })
        return 1

    def deliver_due(
        self, alert_id: int, sender, *, payload: dict[str, object], defer_commit: bool = False
    ) -> bool:
        alert = next(item for item in self.claimed if item["id"] == alert_id)
        delivered = {**alert, **payload}
        self.delivered.append(delivered)
        return bool(sender(delivered))

    def load_schedule_intervals(self) -> dict[str, int]:
        return {}

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def cleanup_steps(self) -> int:
        return 0


class BackendSyncAlertRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        self._old_env = os.environ.copy()
        _clear_worker_app_modules()
        worker_root = str(WORKER_ROOT)
        sys.path[:] = [item for item in sys.path if item != worker_root]
        sys.path.insert(0, worker_root)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        _clear_worker_app_modules()
        sys.path[:] = self._old_sys_path

    def test_backend_no_longer_owns_sync_alert_threads(self) -> None:
        source = OPS.read_text(encoding="utf-8")
        for forbidden in (
            "_chanjet_token_alert_loop", "chanjet-token-watcher",
            "_tplus_full_sync_alert_loop", "tplus-full-sync-watcher",
            "CHANJET_ALERT_FEISHU_RECEIVE_ID",
        ):
            self.assertNotIn(forbidden, source)

    def test_default_worker_invokes_unified_notifier(self) -> None:
        """Deleting the default notifier assembly must fail this test."""
        from app.pipelines import worker_loop

        calls: list[str] = []
        with (
            mock.patch.dict(os.environ, {"DOC_SYNC_POLL_SECONDS": "60"}, clear=False),
            mock.patch.object(worker_loop, "resolve_groupbot_profile", return_value=""),
            mock.patch.object(
                worker_loop,
                "read_schedule_config",
                return_value={"enabled": False, "interval_seconds": 60, "anchor_time": "", "pull_paused": False},
            ),
            mock.patch.object(worker_loop, "read_last_full_run", return_value=None),
            mock.patch.object(worker_loop, "pull_config_from_bitable", return_value="noop"),
            mock.patch.object(worker_loop, "run_sync_wecom_full", return_value=0),
            mock.patch.object(worker_loop, "run_backfill_images", return_value=mock.Mock()),
            mock.patch.object(worker_loop, "run_sync_feishu_full", return_value=0),
            mock.patch.object(worker_loop, "run_pending_document_locator_mirror_jobs", return_value=0),
            mock.patch.object(worker_loop, "run_pending_sync_requests", return_value=0),
            mock.patch.object(worker_loop, "run_write_rnd_records", return_value=0),
            mock.patch.object(worker_loop, "run_tplus_parent_match", return_value=0),
            mock.patch.object(worker_loop, "run_backfill_if_bom_synced", return_value=(None, False)),
            mock.patch.object(
                worker_loop.sync_alert_notifier,
                "run_notifier_once",
                side_effect=lambda: calls.append("notifier") or {},
            ),
        ):
            code = worker_loop.run_worker_loop(
                full_sync=None,
                consume_requests=None,
                sleep=lambda _seconds: None,
                max_cycles=1,
                now_fn=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
            )

        self.assertEqual(0, code)
        self.assertEqual(["notifier"], calls)

    def test_real_notifier_claims_and_delivers_expiring_chanjet_credential(self) -> None:
        """Replacing the notifier credential branch with a no-op must fail this test."""
        from app.pipelines import sync_alert_notifier

        now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
        state: dict[str, object] = {
            "job": {
                "id": 42,
                "job_key": "chanjet.full",
                "display_name": "Synthetic Chanjet",
                "enabled": True,
                "alert_enabled": True,
                "alert_chat_id": "oc_synthetic_retirement",
                "freshness_sla_seconds": None,
                "artifact_glob": None,
            },
            "latest_run": {"id": 7, "status": "success", "detail_json": {}},
            "latest_success": {"id": 7, "started_at": now, "finished_at": now, "detail_json": {}},
            "consecutive_failures": 0,
            "open_alerts": [],
        }
        repository = _CredentialAlertRepository(state, now)
        sent: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"CHANJET_OPEN_TOKEN_FILE": str(Path(tmp) / "synthetic-token.jwt")},
            clear=False,
        ):
            token_path = Path(os.environ["CHANJET_OPEN_TOKEN_FILE"])
            token_path.write_text(_synthetic_jwt(now + timedelta(days=1)), encoding="utf-8")
            result = sync_alert_notifier.run_notifier_once(
                repository=repository,
                sender=lambda chat_id, text: sent.append((chat_id, text)) or True,
                now=now,
            )

        self.assertEqual({"checked": 1, "opened": 1, "notified": 1}, {
            key: result[key] for key in ("checked", "opened", "notified")
        })
        self.assertEqual(["credential_expiring"], [item["alert_kind"] for item in repository.claimed])
        self.assertEqual(["credential_expiring"], [item["alert_kind"] for item in repository.delivered])
        self.assertEqual(["oc_synthetic_retirement"], [chat_id for chat_id, _text in sent])
        self.assertIn("凭据告警", sent[0][1])


if __name__ == "__main__":
    unittest.main()
