from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db" / "migrations" / "0045_txecs_version_inventory.sql"


class TxecsVersionMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_registers_txecs_heartbeat_idempotently(self) -> None:
        self.assertIn("'version-inventory-txecs'", self.sql)
        self.assertIn("ON CONFLICT (code) DO UPDATE", self.sql)

    def test_registers_txecs_postgres_and_tcr_mirrors(self) -> None:
        self.assertIn("'postgres-txecs'", self.sql)
        self.assertIn("ccr.ccs.tencentyun.com/hydwang-infra/backend-api", self.sql)
        self.assertIn("ccr.ccs.tencentyun.com/hydwang-infra/openclaw-bridge", self.sql)


if __name__ == "__main__":
    unittest.main()
