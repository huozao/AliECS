"""全站消息浮层：所有会弹消息的页面必须引入 toast.js，且不再依赖顶部内联横幅。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_WEB_TOAST = ROOT / "services" / "public-web" / "common" / "toast.js"
ADMIN_UI_TOAST = ROOT / "services" / "admin-ui" / "common" / "toast.js"

# 用 AliECSToast 弹消息的页面（相对 services/ 的路径）。
TOAST_PAGES = [
    "public-web/index.html",
    "public-web/exports/index.html",
    "public-web/formula/index.html",
    "public-web/health/index.html",
    "public-web/health/versions/index.html",
    "public-web/inventory/finished-goods/index.html",
    "public-web/inventory/raw-materials/index.html",
    "public-web/bom-builder/index.html",
    "public-web/quality-reports/index.html",
    "public-web/sync/index.html",
    "admin-ui/index.html",
]

# 已被浮层取代的顶部横幅元素，页面里不应再出现。
REMOVED_BANNERS = [
    'id="msg"',
    'id="message"',
    'id="errorBox"',
    'id="successBox"',
]


class FrontendToastTests(unittest.TestCase):
    def page(self, rel: str) -> str:
        return (ROOT / "services" / rel).read_text(encoding="utf-8")

    def test_admin_ui_copy_matches_public_web(self) -> None:
        """admin-ui 独立镜像，只能放副本；两份必须字节一致。"""
        self.assertEqual(
            PUBLIC_WEB_TOAST.read_text(encoding="utf-8"),
            ADMIN_UI_TOAST.read_text(encoding="utf-8"),
        )

    def test_toast_is_fixed_and_mobile_aware(self) -> None:
        source = PUBLIC_WEB_TOAST.read_text(encoding="utf-8")
        self.assertIn("position:fixed", source)
        self.assertIn("env(safe-area-inset-bottom)", source)
        self.assertIn("@media(max-width:600px)", source)

    def test_error_toast_does_not_auto_hide(self) -> None:
        source = PUBLIC_WEB_TOAST.read_text(encoding="utf-8")
        self.assertIn('if (kind !== "error") timer = setTimeout(hide, AUTO_HIDE_MS);', source)

    def test_pages_load_toast_script(self) -> None:
        for rel in TOAST_PAGES:
            with self.subTest(page=rel):
                self.assertIn('<script src="/common/toast.js"></script>', self.page(rel))

    def test_pages_dropped_inline_banner(self) -> None:
        for rel in TOAST_PAGES:
            html = self.page(rel)
            for banner in REMOVED_BANNERS:
                with self.subTest(page=rel, banner=banner):
                    self.assertNotIn(banner, html)

    def test_message_helpers_delegate_to_toast(self) -> None:
        for rel in TOAST_PAGES:
            html = self.page(rel)
            helpers = re.findall(r"function (showError|showSuccess|showMessage|message)\(([^\n]*)", html)
            with self.subTest(page=rel):
                self.assertTrue(helpers, "页面应至少有一个消息函数")
                for _name, body in helpers:
                    self.assertIn("AliECSToast", body)


if __name__ == "__main__":
    unittest.main()
