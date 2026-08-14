from __future__ import annotations

import unittest
from pathlib import Path


EXPORTS_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "exports" / "index.html"


class ExportsFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = EXPORTS_PAGE.read_text(encoding="utf-8")

    def test_is_static_compatibility_redirect(self) -> None:
        self.assertIn('http-equiv="refresh"', self.html)
        self.assertIn("/sync/?view=assets", self.html)
        self.assertNotIn("/v1/exports/catalog", self.html)
        self.assertNotIn("<script", self.html)

    def test_is_download_only_and_points_to_sync_center(self) -> None:
        self.assertIn('href="/sync/?view=assets"', self.html)
        self.assertNotIn("function copyExportDoc(", self.html)
        self.assertNotIn("function syncExportDoc(", self.html)
        self.assertNotIn("function syncAllExports(", self.html)
        self.assertNotIn("method:'POST'", self.html)
        self.assertNotIn("method:'PUT'", self.html)

    def test_has_no_sync_configuration_or_copy_controls(self) -> None:
        for marker in ("docSyncSaveBtn", "docSyncEnabled", "syncAllBtn", "创建副本", "立即同步"):
            self.assertNotIn(marker, self.html)


if __name__ == "__main__":
    unittest.main()
