from __future__ import annotations

import unittest
from pathlib import Path


ADMIN_PAGE = Path(__file__).resolve().parents[1] / "services" / "admin-ui" / "index.html"


class AdminFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = ADMIN_PAGE.read_text(encoding="utf-8")

    def test_audit_logs_are_collapsible_and_paginated(self) -> None:
        self.assertIn('<details id="auditLogPanel"', self.html)
        self.assertIn('<summary><h2>审计日志</h2>', self.html)
        self.assertIn('id="auditPrevBtn"', self.html)
        self.assertIn('id="auditNextBtn"', self.html)
        self.assertIn('id="auditPageInfo"', self.html)
        self.assertIn('auditLogs:{items:[],page:1,page_size:50,total:0}', self.html)
        self.assertIn('api(`/v1/admin/audit-logs?page=${page}&page_size=${state.auditLogs.page_size}`)', self.html)

    def test_system_config_overview_is_rendered(self) -> None:
        self.assertIn('id="systemConfigPanel"', self.html)
        self.assertIn('id="systemConfigBody"', self.html)
        self.assertIn('api("/v1/admin/system-config/effective")', self.html)
        self.assertIn('renderSystemConfig()', self.html)
        self.assertIn('toggleDocSyncPullPaused', self.html)

    def test_links_use_safe_href_helper(self) -> None:
        self.assertIn("function safeHref(value)", self.html)
        self.assertIn('parsed.protocol === "http:" || parsed.protocol === "https:"', self.html)
        self.assertIn('raw.startsWith("/") && !raw.startsWith("//")', self.html)
        self.assertIn('renderSafeLink(f.url, "打开")', self.html)
        self.assertIn('return renderSafeLink(editor.url, editor.label || "打开");', self.html)

    def test_system_config_load_failure_is_panel_scoped(self) -> None:
        self.assertIn("async function loadSystemConfig()", self.html)
        self.assertIn('source: "读取失败"', self.html)
        self.assertNotIn('api("/v1/admin/system-config/effective")\n      ]);', self.html)


if __name__ == "__main__":
    unittest.main()
