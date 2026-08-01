from __future__ import annotations

import unittest
from pathlib import Path


HOME_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "index.html"


class PublicWebHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HOME_PAGE.read_text(encoding="utf-8")

    def test_home_requests_only_features_visible_to_current_user(self) -> None:
        self.assertIn("api('/v1/features')", self.html)
        self.assertNotIn("include_all=true", self.html)
        self.assertNotIn("未登录或无权限", self.html)
        self.assertIn('<a id="adminLink" class="hidden" href="/admin/">', self.html)
        self.assertIn("adminLink.classList.toggle('hidden',!adminAllowed);", self.html)


if __name__ == "__main__":
    unittest.main()
