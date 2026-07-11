from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TPlusBomBuilderFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "services" / "public-web" / "bom-builder" / "index.html").read_text(encoding="utf-8")
        cls.migration = (ROOT / "db" / "migrations" / "0026_tplus_bom_write.sql").read_text(encoding="utf-8")

    def test_page_uses_draft_validate_submit_status_api(self):
        self.assertIn("/v1/tplus/inventories", self.html)
        self.assertIn("/v1/tplus/bom-drafts", self.html)
        self.assertIn("/validate", self.html)
        self.assertIn("/submit", self.html)
        self.assertIn("/v1/tplus/bom-submissions/", self.html)
        self.assertIn("confirmed:true", self.html)

    def test_page_supports_material_scope_quantity_and_custom_inventory(self):
        self.assertIn('value="material" selected>原材料库', self.html)
        self.assertIn("添加子件", self.html)
        self.assertIn("需用数量", self.html)
        self.assertIn("新增自定义存货", self.html)
        self.assertIn("inventory_class_code", self.html)

    def test_migration_adds_permission_feature_and_audit_tables(self):
        self.assertIn("tplus.bom.write", self.migration)
        self.assertIn("tplus_bom_builder", self.migration)
        self.assertIn("tplus_bom_drafts", self.migration)
        self.assertIn("tplus_bom_submissions", self.migration)
        self.assertIn("tplus_bom_submission_events", self.migration)


if __name__ == "__main__":
    unittest.main()
