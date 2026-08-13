from __future__ import annotations

import unittest
from pathlib import Path


EXPORTS_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "exports" / "index.html"


class ExportsFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = EXPORTS_PAGE.read_text(encoding="utf-8")

    def test_has_exports_loader(self) -> None:
        self.assertIn("function loadExports(", self.html)

    def test_calls_catalog_endpoint(self) -> None:
        self.assertIn("/v1/exports/catalog", self.html)

    def test_has_download_export(self) -> None:
        # downloadExport 已抽到 /common/admin-auth.js，页面只保留挂载与错误提示。
        self.assertIn('<script src="/common/admin-auth.js"></script>', self.html)
        self.assertIn("window.downloadExport=", self.html)

    def test_is_download_only_and_points_to_sync_center(self) -> None:
        self.assertIn('href="/sync/"', self.html)
        self.assertNotIn("function copyExportDoc(", self.html)
        self.assertNotIn("function syncExportDoc(", self.html)
        self.assertNotIn("function syncAllExports(", self.html)
        self.assertNotIn("method:'POST'", self.html)
        self.assertNotIn("method:'PUT'", self.html)

    def test_has_back_link_to_health(self) -> None:
        self.assertIn('href="/health/"', self.html)

    def test_has_no_sync_configuration_or_copy_controls(self) -> None:
        for marker in ("docSyncSaveBtn", "docSyncEnabled", "syncAllBtn", "创建副本", "立即同步"):
            self.assertNotIn(marker, self.html)


if __name__ == "__main__":
    unittest.main()
