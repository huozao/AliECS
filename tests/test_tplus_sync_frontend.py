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

    def test_has_sync_config_card(self) -> None:
        self.assertIn('id="syncConfigCard"', self.html)
        self.assertIn('id="syncEnabled"', self.html)
        self.assertIn('id="syncIntervalHours"', self.html)

    def test_has_sync_config_loader_and_saver(self) -> None:
        self.assertIn("function loadSyncConfig(", self.html)
        self.assertIn("function saveSyncConfig(", self.html)

    def test_calls_sync_config_endpoint(self) -> None:
        self.assertIn("/v1/ops/tplus/sync-config", self.html)

    def test_excel_column_opens_excel_only_view(self) -> None:
        # 「生成的 Excel」列按钮只展示文件下载，不再混入变化摘要
        self.assertIn("function openExcelOnly(", self.html)
        self.assertIn("openExcelOnly(", self.html)

    def test_detail_shows_readable_change_not_raw_json(self) -> None:
        # 变化摘要移到「详情」里以可读形式展示，不再倾倒原始 JSON
        self.assertIn("本次变化：", self.html)
        self.assertNotIn("jsonDetailBlock('变化摘要'", self.html)


if __name__ == "__main__":
    unittest.main()
