from __future__ import annotations

import unittest
from pathlib import Path


SYNC_PAGE = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "public-web"
    / "sync"
    / "index.html"
)


class SyncFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = SYNC_PAGE.read_text(encoding="utf-8") if SYNC_PAGE.exists() else ""

    def test_has_summary_jobs_timeline_drawer_and_alert_layers(self) -> None:
        for dom_id in (
            "syncSummary",
            "jobList",
            "timelineList",
            "timelinePager",
            "runDrawer",
            "alertList",
        ):
            self.assertIn(f'id="{dom_id}"', self.html)

    def test_uses_shared_admin_assets_and_gate_contract(self) -> None:
        for marker in (
            'href="/common/admin.css"',
            'src="/common/toast.js"',
            'src="/common/admin-auth.js"',
            'id="loginBtn"',
            'id="logoutBtn"',
            'id="refreshBtn"',
            'id="gateHint"',
            'id="adminContent"',
        ):
            self.assertIn(marker, self.html)
        self.assertIn("AliECSAdmin", self.html)
        self.assertIn("AliECSAdmin.applyGate", self.html)
        self.assertNotIn("async function api(", self.html)
        self.assertNotIn("function fmtTime(", self.html)
        self.assertNotIn("function chip(", self.html)
        self.assertNotIn("--bg:#f7f5f0", self.html)

    def test_uses_only_read_only_sync_endpoints(self) -> None:
        for path in ("/v1/sync/overview", "/v1/sync/runs", "/v1/sync/alerts"):
            self.assertIn(path, self.html)
        for write_marker in ("method:'POST'", 'method: "POST"', "method:'PUT'", 'method: "PUT"'):
            self.assertNotIn(write_marker, self.html)

    def test_has_global_filters_paging_and_query_preselection(self) -> None:
        for dom_id in (
            "providerFilter",
            "statusFilter",
            "jobFilter",
            "timelinePrevBtn",
            "timelineNextBtn",
            "timelinePageInfo",
        ):
            self.assertIn(f'id="{dom_id}"', self.html)
        self.assertIn("URLSearchParams(location.search)", self.html)
        self.assertIn(".get('job')", self.html)
        self.assertIn("params.set('provider'", self.html)
        self.assertIn("params.set('status'", self.html)
        self.assertIn("params.set('job_key'", self.html)
        self.assertIn("state.offset", self.html)

    def test_renders_unmonitored_and_explicit_empty_alert_state(self) -> None:
        self.assertIn("unmonitored", self.html)
        self.assertIn("未监控", self.html)
        self.assertIn("暂无未解决告警", self.html)

    def test_dynamic_api_text_is_escaped_without_raw_detail_dump(self) -> None:
        for field in (
            "display_name",
            "job_key",
            "provider",
            "error_label",
            "error_message",
            "alert_kind",
        ):
            self.assertIn(f"esc(item.{field}", self.html)
        self.assertNotIn("JSON.stringify", self.html)
        self.assertNotIn("detail_json", self.html)

    def test_future_write_actions_are_disabled_without_handlers(self) -> None:
        self.assertIn('disabled title="后续阶段开放"', self.html)
        self.assertNotIn("runJob", self.html)
        self.assertNotIn("saveJob", self.html)


if __name__ == "__main__":
    unittest.main()
