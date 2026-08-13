from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TPLUS_PAGE = ROOT / "services" / "public-web" / "tplus-sync" / "index.html"
NGINX = ROOT / "services" / "public-web" / "nginx.conf"


class TplusSyncFrontendTests(unittest.TestCase):
    def test_nginx_redirects_legacy_page_to_unified_center(self) -> None:
        config = NGINX.read_text(encoding="utf-8")
        self.assertIn("location = /tplus-sync/", config)
        self.assertIn("return 301 /sync/?group=tplus;", config)

    def test_static_fallback_has_no_control_surface(self) -> None:
        html = TPLUS_PAGE.read_text(encoding="utf-8")
        self.assertIn("/sync/?group=tplus", html)
        self.assertNotIn("method:'POST'", html)
        self.assertNotIn("manualFullSyncBtn", html)
        self.assertNotIn("saveSyncConfig", html)


if __name__ == "__main__":
    unittest.main()
