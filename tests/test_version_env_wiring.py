from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EnvWiringTests(unittest.TestCase):
    def test_compose_maps_feishu_and_receive_id(self) -> None:
        c = (ROOT / "deploy" / "ecs" / "compose.prod.yml").read_text(encoding="utf-8")
        for key in ("FEISHU_APP_ID:", "FEISHU_APP_SECRET:", "VERSION_DIGEST_FEISHU_RECEIVE_ID:"):
            self.assertIn(key, c)

    def test_deploy_heredoc_passes_keys(self) -> None:
        d = (ROOT / "deploy" / "ecs" / "deploy.sh").read_text(encoding="utf-8")
        for key in ("FEISHU_APP_ID=", "FEISHU_APP_SECRET=", "VERSION_DIGEST_FEISHU_RECEIVE_ID="):
            self.assertIn(key, d)

    def test_release_meta_example_has_placeholders(self) -> None:
        e = (ROOT / "deploy" / "ecs" / "release-meta.env.example").read_text(encoding="utf-8")
        for key in ("FEISHU_APP_ID=", "FEISHU_APP_SECRET=", "VERSION_DIGEST_FEISHU_RECEIVE_ID="):
            self.assertIn(key, e)

    def test_runtime_env_example_has_placeholders(self) -> None:
        e = (ROOT / "deploy" / "ecs" / "runtime.env.example").read_text(encoding="utf-8")
        for key in ("FEISHU_APP_ID=", "FEISHU_APP_SECRET=", "VERSION_DIGEST_FEISHU_RECEIVE_ID="):
            self.assertIn(key, e)


if __name__ == "__main__":
    unittest.main()
