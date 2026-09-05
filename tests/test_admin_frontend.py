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

    def test_unauthorized_entry_does_not_disclose_permission_name(self) -> None:
        self.assertIn('throw new Error("当前页面不可用。");', self.html)
        self.assertNotIn("当前账号没有 admin.access 权限", self.html)

    def test_clash_profile_panel_is_present(self) -> None:
        self.assertIn('data-target="sec-clash-profile"', self.html)
        self.assertIn('id="sec-clash-profile"', self.html)
        self.assertIn('id="clashProviderBody"', self.html)
        self.assertIn('id="clashDownloadBtn"', self.html)
        self.assertIn('id="clashDownloadWebdockBtn"', self.html)
        self.assertIn('id="clashDownloadMobileBtn"', self.html)
        self.assertIn('id="clashCopyBtn"', self.html)
        self.assertIn('api("/v1/admin/clash-profile/providers")', self.html)

    def test_clash_profile_can_download_webdock_target(self) -> None:
        self.assertIn('downloadClashProfile("webdock")', self.html)
        self.assertIn('downloadClashProfile("mobile")', self.html)
        self.assertIn('clash-profile-mobile.yaml', self.html)
        self.assertIn('target==="desktop"?"":"?target="', self.html)
        self.assertIn("async function loadClashProviders()", self.html)
        self.assertIn("function renderClashProviders()", self.html)

    def test_clash_profile_urls_are_masked_by_default(self) -> None:
        # 订阅 URL 含机场分配的 token，列表默认打码，点开才显示。
        # 只断言 revealed 初值为空，不锁整个 state 字面量——加一个无关字段就会误红。
        self.assertIn("function maskSubscriptionUrl(", self.html)
        self.assertIn("revealed:{}", self.html)
        self.assertIn("state.clashProfile.revealed[p.id]?escapeHtml(p.url)", self.html)

    def test_clash_snapshot_panel_shows_change_time_not_just_fetch_time(self) -> None:
        """后台要显示"节点最后一次变化"，那才对应"需要重新导入"这个动作。

        只显示"上次拉取"没有信息量——它每天都在动，看久了会被忽略，而机场换域名
        （2026-08-15 那次 ss→vless）是无预警的，必须让它显眼。
        """
        self.assertIn('api("/v1/admin/clash-profile/snapshots")', self.html)
        self.assertIn("节点最后一次变化", self.html)
        self.assertIn("data-clash-fetch=", self.html)
        self.assertIn("data-clash-nodes=", self.html)


if __name__ == "__main__":
    unittest.main()
