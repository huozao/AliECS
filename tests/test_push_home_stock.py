from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "services" / "backend-api"
WORKER_ROOT = REPO_ROOT / "services" / "tplus-sync-worker"
MEIDI_AUTO_ROOT = REPO_ROOT.parent / "meidi-auto"
MEIDI_AUTO_VENV_PKGS = MEIDI_AUTO_ROOT / ".venv" / "lib" / "python3.12" / "site-packages"

for p in (str(BACKEND_ROOT), str(WORKER_ROOT / "src"), str(WORKER_ROOT), str(MEIDI_AUTO_ROOT), str(MEIDI_AUTO_VENV_PKGS)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 为 020 顶层执行提供防御假凭据，避免模块加载时阻断
os.environ.setdefault("EMAIL_ADDRESS_QQ", "test@qq.com")
os.environ.setdefault("EMAIL_PASSWORD_QQ", "dummy_pass")

from tplus_datahub.modules.inventory.push_home_stock import (
    filter_finished_stock,
    render_stock_html_table,
    build_home_stock_notification,
    push_home_stock_email,
)


class PushHomeStockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mock_rows = [
            {
                "WarehouseCode": "001",
                "WarehouseName": "原材库",
                "InventoryCode": "10001001",
                "InventoryName": "ABS树脂0215H",
                "Specification": "",
                "UnitName": "kg",
                "ExistingQuantity": "232.0825",
            },
            {
                "WarehouseCode": "002",
                "WarehouseName": "成品库",
                "InventoryCode": "30122",
                "InventoryName": "30122-BX3027海灰色母",
                "Specification": "PP",
                "UnitName": "kg",
                "ExistingQuantity": "100.0000",
            },
            {
                "WarehouseCode": "002",
                "WarehouseName": "成品库",
                "InventoryCode": "20005",
                "InventoryName": "20005-黑色母料",
                "Specification": "PE",
                "UnitName": "包",
                "ExistingQuantity": 50,
            },
        ]

    def test_filter_finished_stock(self) -> None:
        finished = filter_finished_stock(self.mock_rows)
        # 只保留成品库 2 条
        self.assertEqual(len(finished), 2)
        # 按编码升序排列
        self.assertEqual(finished[0]["存货编码"], "20005")
        self.assertEqual(finished[0]["主数量"], "50")
        self.assertEqual(finished[1]["存货编码"], "30122")
        self.assertEqual(finished[1]["主数量"], "100")

    def test_render_html_matches_meidi_auto_020_parser(self) -> None:
        finished = filter_finished_stock(self.mock_rows)
        html_table = render_stock_html_table(finished, timestamp_str="2026-11-19 16:30:00")

        import tempfile
        from importlib.machinery import SourceFileLoader

        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch.object(sys, "argv", ["020 Email download.py", tmp_dir]):
                script_020_path = MEIDI_AUTO_ROOT / "script" / "020 Email download.py"
                loader = SourceFileLoader("script_020", str(script_020_path))
                mod_020 = loader.load_module()

                parsed_data = mod_020.parse_html_table(html_table)
                self.assertTrue(len(parsed_data) >= 3)  # 表头 + 2条数据
                header = parsed_data[0]
                self.assertEqual(header, ["仓库", "存货编码", "存货名称", "规格型号", "主计量", "主数量"])
                row1 = parsed_data[1]
                self.assertEqual(row1[0], "成品库")
                self.assertEqual(row1[1], "20005")
                self.assertEqual(row1[2], "20005-黑色母料")
                self.assertEqual(row1[5], "50")

    def test_notification_subject_contains_waiting_keyword(self) -> None:
        finished = filter_finished_stock(self.mock_rows)
        notification, subject = build_home_stock_notification(finished, date_str="2026-11-19")

        # 必须满足 meidi-auto 020 识别词 KEYWORDS["waiting"] == "等待您查看"
        self.assertIn("等待您查看", subject)
        self.assertIn("2026-11-19", subject)
        self.assertEqual(notification.title, subject)
        self.assertIn("email", notification.channel_payloads)

    def test_push_email_dry_run(self) -> None:
        res = push_home_stock_email(
            self.mock_rows,
            recipients=["test@example.com"],
            dry_run=True,
            date_str="2026-11-19",
        )
        self.assertEqual(res["status"], "dry-run")
        self.assertEqual(res["count"], 2)
        self.assertIn("等待您查看", res["subject"])


if __name__ == "__main__":
    unittest.main()
