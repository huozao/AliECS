from __future__ import annotations

import unittest
from pathlib import Path


HEALTH_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "health" / "index.html"
TPLUS_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "tplus-sync" / "index.html"


class HealthFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HEALTH_PAGE.read_text(encoding="utf-8")

    def test_tplus_recent_requests_have_detail_entry(self) -> None:
        """Timeline detail entry points now live in /tplus-sync/, not /health/."""
        tplus_html = TPLUS_PAGE.read_text(encoding="utf-8")
        self.assertIn("function openTimelineDetail(", tplus_html)
        self.assertIn('onclick="openTimelineDetail(', tplus_html)
        self.assertIn("<th>详情</th>", tplus_html)

    def test_tplus_requests_and_runs_are_on_dedicated_page(self) -> None:
        """The timeline section moved to /tplus-sync/; health has card links."""
        tplus_html = TPLUS_PAGE.read_text(encoding="utf-8")
        self.assertIn("function syncOriginLabel(", tplus_html)
        self.assertIn("手动同步", tplus_html)
        self.assertIn("定时同步", tplus_html)
        self.assertIn("订阅变更同步", tplus_html)

    def test_unified_timeline_replaces_two_tables(self) -> None:
        """Timeline functions live in /tplus-sync/ page, not in health."""
        tplus_html = TPLUS_PAGE.read_text(encoding="utf-8")
        self.assertIn("function loadTplusTimeline(", tplus_html)
        self.assertIn("/v1/ops/tplus/timeline", tplus_html)
        self.assertIn("<th>生成的 Excel</th>", tplus_html)
        self.assertIn("<th>本次变化</th>", tplus_html)
        self.assertIn("function excelCell(", tplus_html)
        self.assertIn("function diffSummaryText(", tplus_html)
        self.assertNotIn("function loadTplusRuns(", tplus_html)
        self.assertNotIn("function loadTplusRequests(", tplus_html)
        self.assertNotIn("<h2>差异校验</h2>", tplus_html)

    def test_timeline_detail_shows_reviewable_change_breakdown(self) -> None:
        """Diff detail + review capability lives in /tplus-sync/."""
        tplus_html = TPLUS_PAGE.read_text(encoding="utf-8")
        self.assertIn("function renderDiffDetail(", tplus_html)
        self.assertIn("function renderChangedRows(", tplus_html)
        self.assertIn("/v1/ops/reconciliation/", tplus_html)
        self.assertIn("标记已复核", tplus_html)
        self.assertIn("变化明细", tplus_html)
        self.assertIn("原数量", tplus_html)
        self.assertIn("新数量", tplus_html)

    def test_health_has_card_links_to_new_pages(self) -> None:
        """Health dashboard has card entries to /tplus-sync/ and /exports/."""
        self.assertIn('href="/tplus-sync/"', self.html)
        self.assertIn('href="/exports/"', self.html)
        self.assertIn("畅捷通同步", self.html)

    def test_health_no_longer_contains_moved_functions(self) -> None:
        """Timeline and exports JS must not remain in health."""
        self.assertNotIn("function loadTplusTimeline(", self.html)
        self.assertNotIn("function loadExports(", self.html)
        self.assertNotIn("function renderTplusTimeline(", self.html)
        self.assertNotIn("function renderExports(", self.html)
        self.assertNotIn("function downloadExport(", self.html)


if __name__ == "__main__":
    unittest.main()
