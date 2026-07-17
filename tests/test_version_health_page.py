from __future__ import annotations
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "services" / "public-web" / "health" / "index.html"


class VersionHealthPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HEALTH.read_text(encoding="utf-8")

    def test_has_versions_section_and_api(self) -> None:
        self.assertIn("版本巡检", self.html)
        self.assertIn("/v1/ops/versions", self.html)

    def test_has_render_function_and_status_badges(self) -> None:
        self.assertIn("function renderVersions(", self.html)
        for label in ("落后", "最新", "未登记"):
            self.assertIn(label, self.html)
