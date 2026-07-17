# tests/test_version_inventory_migration.py
from __future__ import annotations
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "db" / "migrations" / "0037_version_inventory.sql"


class VersionInventoryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = MIG.read_text(encoding="utf-8")

    def test_creates_three_tables(self) -> None:
        for t in ("version_components", "version_reports", "version_upstream_state"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {t}", self.sql)

    def test_seeds_heartbeat_policies_for_each_device(self) -> None:
        for code in ("version-inventory-aliecs", "version-inventory-webdock1", "version-inventory-webdock2"):
            self.assertIn(f"'{code}'", self.sql)
        self.assertIn("ON CONFLICT (code) DO UPDATE", self.sql)

    def test_seeds_pain_point_components(self) -> None:
        for key in ("openclaw", "immich-server", "postgres-aliecs"):
            self.assertIn(f"'{key}'", self.sql)

    def test_pins_postgres_major_version(self) -> None:
        # postgres 锁大版本，避免误报"该升 17"
        self.assertIn("^16", self.sql)

    def test_own_images_have_no_upstream(self) -> None:
        self.assertIn("'own'", self.sql)
