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

    def test_has_copy_export_doc(self) -> None:
        self.assertIn("function copyExportDoc(", self.html)

    def test_has_back_link_to_health(self) -> None:
        self.assertIn('href="/health/"', self.html)

    def test_has_doc_sync_config_card(self) -> None:
        self.assertIn("function loadDocSyncConfig(", self.html)
        self.assertIn("function saveDocSyncConfig(", self.html)
        self.assertIn("/v1/ops/doc-sync/sync-config", self.html)
        self.assertIn('id="docSyncAnchorTime"', self.html)
        self.assertIn('id="docSyncPullPaused"', self.html)
        self.assertIn("loadDocSyncConfig()", self.html)  # applyGate 管理员分支触发加载


if __name__ == "__main__":
    unittest.main()
