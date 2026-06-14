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


if __name__ == "__main__":
    unittest.main()
