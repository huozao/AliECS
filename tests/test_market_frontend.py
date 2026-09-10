from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKET_PAGE = ROOT / "services" / "public-web" / "market" / "index.html"
MARKET_SCRIPT = ROOT / "services" / "public-web" / "market" / "market.js"
MARKET_SPEC = ROOT / "docs" / "superpowers" / "specs" / "2026-09-05-market-dashboard-subdomain-design.md"


class MarketFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = MARKET_PAGE.read_text(encoding="utf-8") + "\n" + MARKET_SCRIPT.read_text(encoding="utf-8")
        self.spec = MARKET_SPEC.read_text(encoding="utf-8")

    def test_page_uses_read_only_snapshot_contract(self) -> None:
        self.assertIn('const SNAPSHOT_API = "/api/v1/market/snapshot";', self.html)
        self.assertIn('cache: "no-store"', self.html)
        # The page may POST a browser-bound, one-use SSO handoff.  It must
        # never submit a market snapshot or invoke a browser trading action.
        self.assertNotIn('fetch(SNAPSHOT_API, {method: "POST"', self.html)
        self.assertNotIn('/v1/market/orders', self.html)
        self.assertNotIn('/v1/market/trade', self.html)
        self.assertIn('/api/v1/market/annotations', self.html)
        self.assertNotIn("/v1/internal/", self.html)

    def test_page_preserves_source_and_ingest_times(self) -> None:
        self.assertIn("source_timestamp", self.html)
        self.assertIn("ingested_at", self.html)
        self.assertIn("comparison_status", self.html)
        self.assertIn('summary.available === false', self.html)

    def test_sso_return_url_keeps_market_subdomain(self) -> None:
        self.assertIn("location.origin", self.html)
        self.assertIn("/api/v1/auth/oidc/login?rd=", self.html)
        self.assertIn("absorbLoginHandoff", self.html)
        self.assertIn("aliecs_auth_token", self.html)
        self.assertIn('id="userBadge"', self.html)
        self.assertIn("renderUserBadge", self.html)
        self.assertIn('id="logout"', self.html)

    def test_page_does_not_render_fake_market_values(self) -> None:
        self.assertIn("当前没有可展示的快照", self.html)
        self.assertIn("不生成演示行情", self.spec)

    def test_page_accepts_v6_candidate_event_fields_and_controls_history(self) -> None:
        self.assertIn("candidate.contract || candidate.symbol", self.html)
        self.assertIn("candidate.expected_cost_cny_per_pair ?? candidate.expected_cost_cny", self.html)
        self.assertIn("selectedWindow", self.html)
        self.assertIn("seriesAfter", self.html)
        self.assertIn("subscribeClick", self.html)
        self.assertIn("setCrosshairPosition", self.html)

    def test_spec_sets_canonical_domain_and_separates_raw_files(self) -> None:
        self.assertIn("market.hydwang.xyz", self.spec)
        self.assertIn("不直接暴露给浏览器", self.spec)
        self.assertIn("DNS-only", self.spec)

    def test_three_page_split_has_bounded_entrypoints(self) -> None:
        realtime = (ROOT / "services/public-web/market/realtime.html").read_text(encoding="utf-8")
        script = (ROOT / "services/public-web/market/realtime.js").read_text(encoding="utf-8")
        today = (ROOT / "services/public-web/market/today.html").read_text(encoding="utf-8")
        history = (ROOT / "services/public-web/market/history.html").read_text(encoding="utf-8")
        detail = (ROOT / "services/public-web/market/review-detail.js").read_text(encoding="utf-8")
        self.assertIn("/api/v1/market/realtime", script)
        self.assertIn("window_minutes", script)
        self.assertIn("/api/v1/market/events/index", detail)
        self.assertIn("/detail", detail)
        for page in (realtime, today, history):
            self.assertIn("/market/realtime/", page)
            self.assertIn("/market/today/", page)
            self.assertIn("/market/history/", page)


if __name__ == "__main__":
    unittest.main()
