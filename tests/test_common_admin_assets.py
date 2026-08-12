from __future__ import annotations

import unittest
from pathlib import Path


PUBLIC_WEB = Path(__file__).resolve().parents[1] / "services" / "public-web"
ADMIN_CSS = PUBLIC_WEB / "common" / "admin.css"
EXPORTS_PAGE = PUBLIC_WEB / "exports" / "index.html"
TPLUS_PAGE = PUBLIC_WEB / "tplus-sync" / "index.html"
SYNC_PAGE = PUBLIC_WEB / "sync" / "index.html"


class AdminCssTests(unittest.TestCase):
    def test_admin_css_exists(self) -> None:
        self.assertTrue(ADMIN_CSS.is_file(), "common/admin.css 缺失")

    def test_admin_css_carries_the_class_contract(self) -> None:
        # P2 的 /sync/ 页会直接用这些类，抽取时不能漏掉任何一个。
        # 计划第 247 行声明的完整类名契约（21 个），不只是最初的抽样 7 个。
        css = ADMIN_CSS.read_text(encoding="utf-8")
        for selector in (
            ".wrap", ".topbar", ".band", ".panel", ".grid", ".btn", ".btn.primary",
            ".chip", ".ok", ".degraded", ".warning", ".critical", ".failed",
            ".muted", ".metric", ".modal", ".modal-panel", ".row", ".list",
            ".attention", ".hidden",
        ):
            self.assertIn(selector, css, f"admin.css 缺少 {selector}")

    def test_admin_pages_link_admin_css(self) -> None:
        for page in (EXPORTS_PAGE, TPLUS_PAGE, SYNC_PAGE):
            html = page.read_text(encoding="utf-8")
            self.assertIn('<link rel="stylesheet" href="/common/admin.css"/>', html,
                          f"{page.name} 未引用 admin.css")

    def test_pages_no_longer_inline_the_shared_css(self) -> None:
        # 抽取的意义就是不留第二份；留着就会各自漂移。
        for page in (EXPORTS_PAGE, TPLUS_PAGE, SYNC_PAGE):
            html = page.read_text(encoding="utf-8")
            self.assertNotIn("--bg:#f7f5f0", html,
                             f"{page.name} 仍内联着共享 CSS 变量")


ADMIN_JS = PUBLIC_WEB / "common" / "admin-auth.js"


class AdminAuthJsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.js = ADMIN_JS.read_text(encoding="utf-8") if ADMIN_JS.is_file() else ""

    def test_admin_auth_js_exists(self) -> None:
        self.assertTrue(ADMIN_JS.is_file(), "common/admin-auth.js 缺失")

    def test_exports_the_documented_surface(self) -> None:
        # P2 的 /sync/ 页按这张契约表调用，少一个就得再抽一次。
        for name in ("API_BASE", "token", "authHeaders", "api", "fetchMe",
                     "isAdminUser", "applyGate", "downloadExport",
                     "clearAuthToken", "ssoLogin", "esc", "fmtTime", "chip"):
            self.assertIn(name, self.js, f"AliECSAdmin 缺少 {name}")

    def test_apply_gate_takes_a_callback(self) -> None:
        # 两页的 applyGate 唯一差异就是管理员分支加载什么，用回调收敛。
        self.assertIn("function applyGate(me, onAdmin)", self.js)

    def test_admin_pages_load_admin_auth_before_use(self) -> None:
        for page in (EXPORTS_PAGE, TPLUS_PAGE, SYNC_PAGE):
            html = page.read_text(encoding="utf-8")
            self.assertIn('<script src="/common/admin-auth.js"></script>', html,
                          f"{page.name} 未引用 admin-auth.js")

    def test_pages_no_longer_define_shared_helpers(self) -> None:
        for page in (EXPORTS_PAGE, TPLUS_PAGE, SYNC_PAGE):
            html = page.read_text(encoding="utf-8")
            self.assertNotIn("async function api(", html,
                             f"{page.name} 仍自带一份 api()")
            self.assertNotIn("async function downloadExport(", html,
                             f"{page.name} 仍自带一份 downloadExport()")

    def test_pages_still_pass_their_own_admin_loader(self) -> None:
        # 抽取不能把「登录后加载什么」丢掉。
        exports_html = EXPORTS_PAGE.read_text(encoding="utf-8")
        self.assertIn("loadDocSyncConfig", exports_html)
        self.assertIn("loadExports", exports_html)
        tplus_html = TPLUS_PAGE.read_text(encoding="utf-8")
        self.assertIn("loadSyncConfig", tplus_html)
        self.assertIn("loadTplusTimeline", tplus_html)


if __name__ == "__main__":
    unittest.main()
