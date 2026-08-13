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
TOKEN_VOLUME = "tplus_sync_requests:/app/tplus-sync-requests:ro"


class DocWorkerComposeEnvTests(unittest.TestCase):
    @staticmethod
    def _service_block(compose: str, service: str) -> str:
        lines = compose.splitlines()
        start = lines.index(f"  {service}:") + 1
        end = next(
            (index for index in range(start, len(lines)) if lines[index].startswith("  ") and not lines[index].startswith("    ")),
            len(lines),
        )
        return "\n".join(lines[start:end])

    @staticmethod
    def _volume_for_target(service_block: str, target: str) -> tuple[str, bool]:
        for line in service_block.splitlines():
            value = line.strip().removeprefix("- ")
            parts = value.split(":")
            if len(parts) >= 2 and parts[1] == target:
                return parts[0], len(parts) >= 3 and parts[2] == "ro"
        raise AssertionError(f"volume target not found: {target}")

    def _assert_doc_worker_env(self, relative_path: str) -> tuple[str, str]:
        compose = (ROOT / relative_path).read_text(encoding="utf-8")
        doc_worker = self._service_block(compose, "doc-sync-worker")
        for expected in (*ALERT_ENV, TOKEN_VOLUME):
            with self.subTest(path=relative_path, expected=expected):
                self.assertIn(expected, doc_worker)
        return compose, doc_worker

    def test_local_doc_worker_reads_the_producer_bind_source_read_only(self) -> None:
        relative_path = "local/docker-compose.local.yml"
        compose, doc_worker = self._assert_doc_worker_env(relative_path)
        producer = self._service_block(compose, "tplus-sync-worker")
        producer_source, _producer_read_only = self._volume_for_target(producer, "/app/output")
        consumer_source, consumer_read_only = self._volume_for_target(doc_worker, "/app/tplus-output")

        compose_dir = (ROOT / relative_path).parent
        self.assertEqual((compose_dir / producer_source).resolve(), (compose_dir / consumer_source).resolve())
        self.assertTrue(consumer_read_only)

    def test_production_doc_worker_reads_the_producer_named_volume_read_only(self) -> None:
        relative_path = "deploy/ecs/compose.prod.yml"
        compose, doc_worker = self._assert_doc_worker_env(relative_path)
        producer = self._service_block(compose, "tplus-sync-worker")
        producer_source, _producer_read_only = self._volume_for_target(producer, "/app/output")
        consumer_source, consumer_read_only = self._volume_for_target(doc_worker, "/app/tplus-output")

        self.assertEqual("tplus_sync_output", producer_source)
        self.assertEqual(producer_source, consumer_source)
        self.assertTrue(consumer_read_only)

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
