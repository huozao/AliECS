from __future__ import annotations

import json
import os
import subprocess
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
    def _render_compose(
        relative_path: str,
        *,
        env_file: str | None = None,
        profiles: tuple[str, ...] = (),
        overrides: dict[str, str] | None = None,
    ) -> dict[str, object]:
        environment = os.environ.copy()
        environment.pop("DOC_SYNC_SCHEDULER_MODE", None)
        environment.pop("TPLUS_SYNC_SCHEDULER_MODE", None)
        if overrides:
            environment.update(overrides)

        command = ["docker", "compose"]
        if env_file:
            command.extend(["--env-file", env_file])
        for profile in profiles:
            command.extend(["--profile", profile])
        command.extend(["-f", relative_path, "config", "--format", "json"])
        rendered = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return json.loads(rendered.stdout)

    @classmethod
    def _scheduler_modes(cls, rendered: dict[str, object]) -> tuple[str, str]:
        services = rendered["services"]
        assert isinstance(services, dict)
        doc_environment = services["doc-sync-worker"]["environment"]
        tplus_environment = services["tplus-sync-worker"]["environment"]
        return doc_environment["SYNC_SCHEDULER_MODE"], tplus_environment["SYNC_SCHEDULER_MODE"]

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

    def test_local_rendered_scheduler_modes_are_independent(self) -> None:
        rendered = self._render_compose(
            "local/docker-compose.local.yml",
            profiles=("tplus",),
            overrides={
                "DOC_SYNC_SCHEDULER_MODE": "shadow",
                "TPLUS_SYNC_SCHEDULER_MODE": "shadow",
            },
        )
        self.assertEqual(("shadow", "shadow"), self._scheduler_modes(rendered))

        rendered = self._render_compose(
            "local/docker-compose.local.yml",
            profiles=("tplus",),
            overrides={
                "DOC_SYNC_SCHEDULER_MODE": "active",
                "TPLUS_SYNC_SCHEDULER_MODE": "shadow",
            },
        )
        self.assertEqual(("active", "shadow"), self._scheduler_modes(rendered))

    def test_production_role_renders_checked_in_shadow_scheduler_modes(self) -> None:
        rendered = self._render_compose(
            "deploy/ecs/compose.business-cn.yml",
            env_file="deploy/ecs/release-meta.env.example",
            overrides={
                "PUBLIC_WEB_IMAGE": "example/public-web",
                "ADMIN_UI_IMAGE": "example/admin-ui",
                "BACKEND_API_IMAGE": "example/backend-api",
                "DOC_SYNC_WORKER_IMAGE": "example/doc-sync-worker",
                "TPLUS_SYNC_WORKER_IMAGE": "example/tplus-sync-worker",
            },
        )
        self.assertEqual(("shadow", "shadow"), self._scheduler_modes(rendered))

    def test_production_runtime_example_keeps_scheduler_modes_independent(self) -> None:
        rendered = self._render_compose(
            "deploy/ecs/compose.business-cn.yml",
            env_file="deploy/ecs/runtime.env.example",
        )
        self.assertEqual(("shadow", "shadow"), self._scheduler_modes(rendered))

        doc_active = self._render_compose(
            "deploy/ecs/compose.business-cn.yml",
            env_file="deploy/ecs/runtime.env.example",
            overrides={"DOC_SYNC_SCHEDULER_MODE": "active"},
        )
        self.assertEqual(("active", "shadow"), self._scheduler_modes(doc_active))

        tplus_legacy = self._render_compose(
            "deploy/ecs/compose.business-cn.yml",
            env_file="deploy/ecs/runtime.env.example",
            overrides={"TPLUS_SYNC_SCHEDULER_MODE": "legacy"},
        )
        self.assertEqual(("shadow", "legacy"), self._scheduler_modes(tplus_legacy))

    def test_scheduler_mode_defaults_are_legacy_when_host_inputs_are_missing(self) -> None:
        local = self._render_compose("local/docker-compose.local.yml", profiles=("tplus",))
        production = self._render_compose(
            "deploy/ecs/compose.business-cn.yml",
            overrides={
                "PUBLIC_WEB_IMAGE": "example/public-web",
                "ADMIN_UI_IMAGE": "example/admin-ui",
                "BACKEND_API_IMAGE": "example/backend-api",
                "DOC_SYNC_WORKER_IMAGE": "example/doc-sync-worker",
                "TPLUS_SYNC_WORKER_IMAGE": "example/tplus-sync-worker",
            },
        )
        self.assertEqual(("legacy", "legacy"), self._scheduler_modes(local))
        self.assertEqual(("legacy", "legacy"), self._scheduler_modes(production))

    def test_release_meta_example_explicitly_selects_shadow_per_worker(self) -> None:
        example = (ROOT / "deploy/ecs/release-meta.env.example").read_text(encoding="utf-8")
        values = dict(
            line.split("=", 1)
            for line in example.splitlines()
            if line.startswith(("DOC_SYNC_SCHEDULER_MODE=", "TPLUS_SYNC_SCHEDULER_MODE="))
        )
        self.assertEqual(
            {
                "DOC_SYNC_SCHEDULER_MODE": "shadow",
                "TPLUS_SYNC_SCHEDULER_MODE": "shadow",
            },
            values,
        )


if __name__ == "__main__":
    unittest.main()
