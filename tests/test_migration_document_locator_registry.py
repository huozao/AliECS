from __future__ import annotations

import re
import unittest
from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "db" / "migrations" / "0050_document_locator_registry.sql"


class DocumentLocatorRegistryMigrationTests(unittest.TestCase):
    def test_migration_exists(self) -> None:
        self.assertTrue(MIGRATION.is_file(), "migration 0050 must exist")

    def test_schema_pins_private_locator_contract(self) -> None:
        self.assertTrue(MIGRATION.is_file(), "migration 0050 must exist")
        sql = MIGRATION.read_text(encoding="utf-8")
        for table in (
            "document_locator_registry",
            "document_locator_events",
            "document_locator_mirror_jobs",
            "document_copy_requests",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
        for fragment in (
            "api_doc_id TEXT",
            "share_ref TEXT",
            "admin_userids JSONB NOT NULL",
            "capabilities JSONB NOT NULL",
            "external_source_id BIGINT REFERENCES external_sources(id) ON DELETE SET NULL",
            "locator_version INTEGER NOT NULL DEFAULT 1",
            "idempotency_key TEXT NOT NULL UNIQUE",
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        ):
            self.assertIn(fragment, sql)
        self.assertIn("WHERE api_doc_id IS NOT NULL", sql)
        self.assertIn("WHERE share_ref IS NOT NULL", sql)
        self.assertNotRegex(sql, r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(?!IF NOT EXISTS)")

    def test_status_vocabularies_and_identity_separation_are_checked(self) -> None:
        self.assertTrue(MIGRATION.is_file(), "migration 0050 must exist")
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CHECK (lifecycle_status IN ('active', 'disabled', 'unresolved'))", sql)
        self.assertIn(
            "CHECK (syncability_status IN ('verified', 'unverified', 'invalid-id', 'permission-denied'))",
            sql,
        )
        self.assertIn("CHECK (api_doc_id IS NOT NULL OR share_ref IS NOT NULL)", sql)
        self.assertIn("CHECK (status IN ('pending', 'running', 'success', 'failed'))", sql)
        self.assertIn("CHECK (status IN ('prepared', 'creating', 'copying', 'external_created', 'registered', 'failed'))", sql)
        self.assertNotRegex(sql, re.compile(r"api_doc_id\s*=\s*share_ref", re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
