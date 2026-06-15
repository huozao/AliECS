from __future__ import annotations

import unittest
from pathlib import Path


HEALTH_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "health" / "index.html"


class HealthFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HEALTH_PAGE.read_text(encoding="utf-8")

    def test_tplus_recent_requests_have_detail_entry(self) -> None:
        self.assertIn("function openTplusRequestDetail(", self.html)
        self.assertIn("onclick=\"openTplusRequestDetail(", self.html)
        self.assertIn("<th>详情</th>", self.html)
        self.assertIn("请求目标", self.html)
        self.assertIn("执行结果", self.html)


if __name__ == "__main__":
    unittest.main()
