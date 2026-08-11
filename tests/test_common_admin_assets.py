from __future__ import annotations

import unittest
from pathlib import Path


PUBLIC_WEB = Path(__file__).resolve().parents[1] / "services" / "public-web"
ADMIN_CSS = PUBLIC_WEB / "common" / "admin.css"
EXPORTS_PAGE = PUBLIC_WEB / "exports" / "index.html"
TPLUS_PAGE = PUBLIC_WEB / "tplus-sync" / "index.html"


class AdminCssTests(unittest.TestCase):
    def test_admin_css_exists(self) -> None:
        self.assertTrue(ADMIN_CSS.is_file(), "common/admin.css 缺失")

    def test_admin_css_carries_the_class_contract(self) -> None:
        # P2 的 /sync/ 页会直接用这些类，抽取时不能漏掉任何一个。
        css = ADMIN_CSS.read_text(encoding="utf-8")
        for selector in (".topbar", ".band", ".btn", ".chip", ".modal", ".hidden", ".muted"):
            self.assertIn(selector, css, f"admin.css 缺少 {selector}")

    def test_both_pages_link_admin_css(self) -> None:
        for page in (EXPORTS_PAGE, TPLUS_PAGE):
            html = page.read_text(encoding="utf-8")
            self.assertIn('<link rel="stylesheet" href="/common/admin.css"/>', html,
                          f"{page.name} 未引用 admin.css")

    def test_pages_no_longer_inline_the_shared_css(self) -> None:
        # 抽取的意义就是不留第二份；留着就会各自漂移。
        for page in (EXPORTS_PAGE, TPLUS_PAGE):
            html = page.read_text(encoding="utf-8")
            self.assertNotIn("--bg:#f7f5f0", html,
                             f"{page.name} 仍内联着共享 CSS 变量")


if __name__ == "__main__":
    unittest.main()
