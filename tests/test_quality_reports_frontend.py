from __future__ import annotations

import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "quality-reports" / "index.html"
HOME = Path(__file__).resolve().parents[1] / "services" / "public-web" / "index.html"


class QualityReportsFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = PAGE.read_text(encoding="utf-8")

    def test_home_has_quality_report_icon(self) -> None:
        self.assertIn("quality_reports:'📄'", HOME.read_text(encoding="utf-8"))

    def test_query_and_detail_endpoints(self) -> None:
        self.assertIn("/v1/quality-reports?", self.html)
        self.assertIn("/v1/quality-reports/${id}", self.html)

    def test_download_uses_authenticated_api(self) -> None:
        self.assertIn("/v1/quality-reports/files/${id}/download", self.html)
        self.assertIn("'X-Client-Channel']='website'", self.html)

    def test_management_flow_is_present(self) -> None:
        self.assertIn("function createDraft(", self.html)
        self.assertIn("function uploadFile(", self.html)
        self.assertIn("function publishDraft(", self.html)

    def test_inputs_have_non_clipping_height(self) -> None:
        self.assertIn("min-height:46px", self.html)
        self.assertIn("line-height:1.45", self.html)
        self.assertIn("box-sizing:border-box", self.html)


if __name__ == "__main__":
    unittest.main()
