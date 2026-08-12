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

    def test_detail_explains_why_no_diff_instead_of_silently_showing_summary(self) -> None:
        """无明细时要说清是「本次没变化」还是「老记录」，不能只甩一行汇总。"""
        self.assertIn("本次内容无变化，无明细", self.html)
        self.assertIn("该记录早于明细留存", self.html)
        self.assertIn("function hasDiff(", self.html)

    def test_needs_review_no_longer_paints_the_row(self) -> None:
        """「需复核」只是分级，不该把整行标红当故障看。"""
        self.assertNotIn("需复核</span>", self.html)
        self.assertNotIn("background:#f9eceb", self.html)

    def test_has_download_export(self) -> None:
        # downloadExport 已抽到 /common/admin-auth.js，页面只保留挂载与错误提示。
        self.assertIn('<script src="/common/admin-auth.js"></script>', self.html)
        self.assertIn("window.downloadExport=", self.html)

    def test_has_count_style_excel_column(self) -> None:
        self.assertIn("openTimelineDetail", self.html)
        self.assertIn("个</button>", self.html)

    def test_has_back_link_to_health(self) -> None:
        self.assertIn('href="/health/"', self.html)

    def test_has_manual_full_sync_button_and_endpoint(self) -> None:
        self.assertIn('id="manualFullSyncBtn"', self.html)
        self.assertIn("function triggerManualFullSync(", self.html)
        self.assertIn("/v1/ops/tplus/full-sync", self.html)
        self.assertIn("manualFullSyncBtn.onclick", self.html)

    def test_manual_full_sync_refreshes_timeline_after_worker_picks_it_up(self) -> None:
        """worker 最长等一个轮询周期(30s)才开始、全量再跑 1~2 分钟，立刻刷新只会看到空的。"""
        self.assertIn("scheduleTimelineRefresh(", self.html)
        self.assertIn("[30000,90000,150000]", self.html)

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
