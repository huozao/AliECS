from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALERT_ENV = (
    "SYNC_ALERT_CHAT_ID: ${SYNC_ALERT_CHAT_ID:-}",
    "SYNC_ALERT_FEISHU_PROFILE: ${SYNC_ALERT_FEISHU_PROFILE:-COMPANY_A}",
    "SYNC_ALERT_ESCALATION_SECONDS: ${SYNC_ALERT_ESCALATION_SECONDS:-21600}",
    "SYNC_ARTIFACT_GRACE_SECONDS: ${SYNC_ARTIFACT_GRACE_SECONDS:-300}",
    "CHANJET_OPEN_TOKEN_FILE: /app/tplus-sync-requests/chanjet_open_token.txt",
)
READ_ONLY_VOLUMES = (
    "tplus_sync_requests:/app/tplus-sync-requests:ro",
    "tplus_sync_output:/app/tplus-output:ro",
)


class DocWorkerComposeEnvTests(unittest.TestCase):
    def _assert_doc_worker_contract(self, relative_path: str) -> None:
        compose = (ROOT / relative_path).read_text(encoding="utf-8")
        doc_worker = compose.split("  doc-sync-worker:", 1)[1].split("\n  tplus-sync-worker:", 1)[0]
        for expected in (*ALERT_ENV, *READ_ONLY_VOLUMES):
            with self.subTest(path=relative_path, expected=expected):
                self.assertIn(expected, doc_worker)

    def test_local_doc_worker_has_alert_env_and_read_only_artifacts(self) -> None:
        self._assert_doc_worker_contract("local/docker-compose.local.yml")

    def test_production_doc_worker_has_alert_env_and_read_only_artifacts(self) -> None:
        self._assert_doc_worker_contract("deploy/ecs/compose.prod.yml")

    def test_deploy_current_env_forwards_all_alert_settings_serially(self) -> None:
        deploy = (ROOT / "deploy/ecs/deploy.sh").read_text(encoding="utf-8")
        current_env = deploy.split('cat > "$CURRENT_ENV" <<ENV', 1)[1].split("\nENV", 1)[0]
        expected = (
            "SYNC_ALERT_CHAT_ID=${SYNC_ALERT_CHAT_ID:-}",
            "SYNC_ALERT_FEISHU_PROFILE=${SYNC_ALERT_FEISHU_PROFILE:-COMPANY_A}",
            "SYNC_ALERT_ESCALATION_SECONDS=${SYNC_ALERT_ESCALATION_SECONDS:-21600}",
            "SYNC_ARTIFACT_GRACE_SECONDS=${SYNC_ARTIFACT_GRACE_SECONDS:-300}",
        )
        positions = [current_env.index(item) for item in expected]
        self.assertEqual(sorted(positions), positions)

    def test_release_meta_example_uses_only_safe_alert_placeholders(self) -> None:
        example = (ROOT / "deploy/ecs/release-meta.env.example").read_text(encoding="utf-8")
        expected = {
            "SYNC_ALERT_CHAT_ID": "",
            "SYNC_ALERT_FEISHU_PROFILE": "COMPANY_A",
            "SYNC_ALERT_ESCALATION_SECONDS": "21600",
            "SYNC_ARTIFACT_GRACE_SECONDS": "300",
        }
        actual = {}
        for line in example.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in expected:
                actual[key] = value
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
