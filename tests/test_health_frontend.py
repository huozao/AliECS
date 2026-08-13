from __future__ import annotations

import unittest
from pathlib import Path


HEALTH_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "health" / "index.html"


class HealthFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HEALTH_PAGE.read_text(encoding="utf-8")

    def test_health_has_card_links_to_new_pages(self) -> None:
        """Health dashboard keeps old cards and adds the read-only sync center."""
        self.assertIn('href="/sync/?group=tplus"', self.html)
        self.assertIn('href="/exports/"', self.html)
        self.assertIn('href="/sync/"', self.html)
        # #188 移动端重设计把「畅捷通同步」卡片标题改为「T+ 同步时间线」，归入「同步与工具」分区
        self.assertIn("T+ 同步", self.html)
        self.assertIn("统一同步中心", self.html)

    def test_health_no_longer_contains_moved_functions(self) -> None:
        """Timeline and exports JS must not remain in health."""
        self.assertNotIn("function loadTplusTimeline(", self.html)
        self.assertNotIn("function loadExports(", self.html)
        self.assertNotIn("function renderTplusTimeline(", self.html)
        self.assertNotIn("function renderExports(", self.html)
        self.assertNotIn("function downloadExport(", self.html)


if __name__ == "__main__":
    unittest.main()
