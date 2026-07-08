from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class BackendSystemConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]

    def setUp(self) -> None:
        self._old_database_url = os.environ.get("DATABASE_URL")
        os.environ.pop("DATABASE_URL", None)

    def tearDown(self) -> None:
        if self._old_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._old_database_url

    def test_effective_config_returns_fallback_ready_shape_without_database(self) -> None:
        from app.routers.system_config import effective_system_config

        result = effective_system_config(_={})

        domains = {item["domain"]: item for item in result["items"]}
        self.assertIn("doc_sync", domains)
        self.assertIn("chat_mode", domains)
        self.assertIn("tplus_export", domains)
        self.assertIn("inventory_warehouse", domains)
        self.assertIn("features", domains)
        self.assertTrue(any(row["key"] == "doc_sync.schedule" for row in domains["doc_sync"]["rows"]))
        self.assertFalse(domains["doc_sync"]["emergency"]["pause_supported"])
        self.assertFalse(domains["doc_sync"]["emergency"]["override_supported"])
        self.assertFalse(domains["doc_sync"]["emergency"]["pull_paused"])

    def test_effective_config_marks_system_config_mirror_sources(self) -> None:
        from app.routers import system_config

        def fake_record(sheet_name: str) -> dict[str, object]:
            if sheet_name == "对话模式":
                return {"配置编号": "global-default", "对话模式默认": "均衡"}
            if sheet_name == "T+导出说明":
                return {"配置编号": "global-default", "bom": "BOM 自定义说明"}
            if sheet_name == "库存仓库范围":
                return {"配置编号": "global-default", "原料仓库": ["001", "012"], "成品排除仓库": ["001"]}
            return {}

        with (
            patch.object(system_config.exports, "_system_config_record", side_effect=fake_record),
            patch.object(system_config.exports, "_inventory_scope_config", return_value=({"001", "012"}, {"001"})),
            patch.object(system_config, "_latest_system_config_sync_at", return_value="2026-07-08T10:00:00+00:00"),
            patch.object(system_config, "_database_available", return_value=True),
        ):
            result = system_config.effective_system_config(_={})

        domains = {item["domain"]: item for item in result["items"]}
        self.assertEqual("系统配置镜像", domains["chat_mode"]["source"])
        self.assertEqual("系统配置镜像", domains["tplus_export"]["source"])
        self.assertEqual("系统配置镜像", domains["inventory_warehouse"]["source"])
        self.assertEqual("2026-07-08T10:00:00+00:00", domains["chat_mode"]["last_synced_at"])
        self.assertTrue(any(row["value"] == "均衡" for row in domains["chat_mode"]["rows"]))
        self.assertTrue(domains["doc_sync"]["emergency"]["pause_supported"])


if __name__ == "__main__":
    unittest.main()
