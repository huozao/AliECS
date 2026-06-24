from __future__ import annotations

import unittest
from pathlib import Path


TPLUS_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "tplus-sync" / "index.html"


class TplusSyncFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = TPLUS_PAGE.read_text(encoding="utf-8")

    def test_has_timeline_loader(self) -> None:
        self.assertIn("function loadTplusTimeline(", self.html)

    def test_calls_timeline_endpoint(self) -> None:
        self.assertIn("/v1/ops/tplus/timeline", self.html)

    def test_has_diff_detail_renderer(self) -> None:
        self.assertIn("function renderDiffDetail(", self.html)

    def test_has_review_action(self) -> None:
        self.assertIn("标记已复核", self.html)

    def test_has_download_export(self) -> None:
        self.assertIn("function downloadExport(", self.html)

    def test_has_count_style_excel_column(self) -> None:
        self.assertIn("openTimelineDetail", self.html)
        self.assertIn("个</button>", self.html)

    def test_has_back_link_to_health(self) -> None:
        self.assertIn('href="/health/"', self.html)


if __name__ == "__main__":
    unittest.main()
