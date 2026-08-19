from __future__ import annotations

import unittest
from pathlib import Path


HEALTH_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "health" / "index.html"


class HealthFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HEALTH_PAGE.read_text(encoding="utf-8")

    def test_health_links_to_the_single_sync_entry(self) -> None:
        """同步区只留一个入口。

        ⚠️ 本用例 2026-08-19 前叫 test_health_has_card_links_to_new_pages，断言的是
        「保留 /sync/?group=tplus 和 /exports/ 两张旧卡片」。该断言自此失效：那两张卡
        与「统一同步中心」落在同一个页面（/exports/ 由 nginx 301 到 /sync/?view=assets），
        三张卡并列只是让人以为有三个去处。nginx 的 301 保留，所以旧链接照旧可用——
        本用例只管 health 页面自己不再并列重复入口。
        """
        self.assertIn('href="/sync/"', self.html)
        self.assertIn("统一同步中心", self.html)
        self.assertNotIn('href="/sync/?group=tplus"', self.html)
        self.assertNotIn('href="/exports/"', self.html)

    def test_health_has_no_dangling_button_bindings(self) -> None:
        """删掉的按钮不能在 JS 里留下引用：未定义变量会让整段脚本停在那里。"""
        self.assertNotIn("addWechatPanelBtn", self.html)

    def test_health_no_longer_contains_moved_functions(self) -> None:
        """Timeline and exports JS must not remain in health."""
        self.assertNotIn("function loadTplusTimeline(", self.html)
        self.assertNotIn("function loadExports(", self.html)
        self.assertNotIn("function renderTplusTimeline(", self.html)
        self.assertNotIn("function renderExports(", self.html)
        self.assertNotIn("function downloadExport(", self.html)


if __name__ == "__main__":
    unittest.main()
