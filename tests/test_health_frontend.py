from __future__ import annotations

import unittest
from pathlib import Path


HEALTH_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "health" / "index.html"


class HealthFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HEALTH_PAGE.read_text(encoding="utf-8")

    def test_tplus_recent_requests_have_detail_entry(self) -> None:
        self.assertIn("function openTimelineDetail(", self.html)
        self.assertIn('onclick="openTimelineDetail(', self.html)
        self.assertIn("<th>详情</th>", self.html)

    def test_tplus_requests_and_runs_are_one_labeled_module(self) -> None:
        self.assertIn('<section class="band" id="tplusSyncModule">', self.html)
        self.assertIn("<h2>T+ 同步</h2>", self.html)
        self.assertNotIn("T+ 同步请求（畅捷通回调触发）", self.html)
        self.assertNotIn("T+ 同步执行记录（全部", self.html)
        self.assertIn("function syncOriginLabel(", self.html)
        self.assertIn("手动同步", self.html)
        self.assertIn("定时同步", self.html)
        self.assertIn("订阅变更同步", self.html)


    def test_unified_timeline_replaces_two_tables(self) -> None:
        self.assertIn('<section class="band" id="tplusSyncModule">', self.html)
        self.assertIn("<h2>T+ 同步</h2>", self.html)
        self.assertIn("function loadTplusTimeline(", self.html)
        self.assertIn("/v1/ops/tplus/timeline", self.html)
        self.assertIn("<th>生成的 Excel</th>", self.html)
        self.assertIn("<th>本次变化</th>", self.html)
        self.assertIn("function excelCell(", self.html)
        self.assertIn("function diffSummaryText(", self.html)
        self.assertNotIn("function loadTplusRuns(", self.html)
        self.assertNotIn("function loadTplusRequests(", self.html)
        self.assertNotIn("<h2>差异校验</h2>", self.html)


if __name__ == "__main__":
    unittest.main()
