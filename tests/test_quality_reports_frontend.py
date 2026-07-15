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
        self.assertIn("/v1/quality-reports/catalog", self.html)
        self.assertIn("/v1/quality-reports/subjects", self.html)
        self.assertIn("/v1/quality-reports/manage/drafts", self.html)

    def test_file_selection_auto_uploads_and_publish_waits_for_server(self) -> None:
        self.assertIn("$('reportFile').onchange", self.html)
        self.assertIn("服务器已确认", self.html)
        self.assertIn('id="publishBtn" class="primary hidden" type="button" disabled', self.html)

    def test_mobile_message_is_visible_near_bottom(self) -> None:
        self.assertIn("bottom:calc(16px + env(safe-area-inset-bottom))", self.html)

    def test_report_number_and_classification_are_separated(self) -> None:
        self.assertIn("系统编号", self.html)
        self.assertIn('name="external_report_no"', self.html)
        self.assertIn('name="report_source_code"', self.html)
        self.assertIn('name="document_type_code"', self.html)
        self.assertIn("selectedTestItems", self.html)

    def test_inputs_have_non_clipping_height(self) -> None:
        self.assertIn("min-height:46px", self.html)
        self.assertIn("line-height:1.45", self.html)
        self.assertIn("box-sizing:border-box", self.html)


if __name__ == "__main__":
    unittest.main()
