from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKET_PAGE = ROOT / "services" / "public-web" / "market" / "index.html"
MARKET_SPEC = ROOT / "docs" / "superpowers" / "specs" / "2026-09-05-market-dashboard-subdomain-design.md"


class MarketFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = MARKET_PAGE.read_text(encoding="utf-8")
        self.spec = MARKET_SPEC.read_text(encoding="utf-8")

    def test_page_uses_read_only_snapshot_contract(self) -> None:
        self.assertIn('const SNAPSHOT_API = "/api/v1/market/snapshot";', self.html)
        self.assertIn('cache: "no-store"', self.html)
        self.assertNotIn("method: \"POST\"", self.html)
        self.assertNotIn("/v1/internal/", self.html)

    def test_page_preserves_source_and_ingest_times(self) -> None:
        self.assertIn("source_timestamp", self.html)
        self.assertIn("ingested_at", self.html)
        self.assertIn("comparison_status", self.html)

    def test_page_does_not_render_fake_market_values(self) -> None:
        self.assertIn("当前没有可展示的快照", self.html)
        self.assertIn("不生成演示行情", self.spec)

    def test_spec_sets_canonical_domain_and_separates_raw_files(self) -> None:
        self.assertIn("market.hydwang.xyz", self.spec)
        self.assertIn("不直接暴露给浏览器", self.spec)
        self.assertIn("DNS-only", self.spec)


if __name__ == "__main__":
    unittest.main()
