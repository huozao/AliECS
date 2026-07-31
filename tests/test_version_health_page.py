from __future__ import annotations
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "services" / "public-web" / "health" / "index.html"
VERSIONS = ROOT / "services" / "public-web" / "health" / "versions" / "index.html"


class VersionHealthPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HEALTH.read_text(encoding="utf-8")

    def test_has_versions_summary_and_api(self) -> None:
        self.assertIn("版本巡检", self.html)
        self.assertIn("/v1/ops/versions", self.html)

    def test_has_detail_entry_link(self) -> None:
        self.assertIn('href="/health/versions/"', self.html)

    def test_no_inline_device_tables(self) -> None:
        self.assertNotIn("versionDevices", self.html)


class VersionDetailPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = VERSIONS.read_text(encoding="utf-8")

    def test_has_render_function_and_api(self) -> None:
        self.assertIn("function renderVersions(", self.html)
        self.assertIn("/v1/ops/versions", self.html)
        self.assertIn('id="driftDevices"', self.html)

    def test_has_status_badges(self) -> None:
        for label in ("落后", "最新", "未登记"):
            self.assertIn(label, self.html)

    def test_has_back_link(self) -> None:
        self.assertIn('href="/health/"', self.html)


if __name__ == "__main__":
    unittest.main()
