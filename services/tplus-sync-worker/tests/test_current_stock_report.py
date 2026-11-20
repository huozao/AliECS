from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from config.settings import Settings
from tplus_datahub.modules.current_stock.export_current_stock import export_current_stock
from tplus_datahub.modules.current_stock.sync_current_stock import sync_current_stock


class CurrentStockReportSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.output_dir = Path(self.temp_dir.name) / "output"
        self.settings = Settings(
            base_url="https://mock.chanjet.com",
            app_key="mock_key",
            app_secret="mock_secret",
            open_token="mock_token",
            default_page_size=200,
            timeout_connect=5,
            timeout_read=30,
            output_dir=str(self.output_dir),
            data_dir=str(self.data_dir),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sync_current_stock_parses_detail_rows(self):
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "Status": 0,
            "Pages": 1,
            "TaskSessionID": "mock-session-1",
            "SolutionID": "1084",
            "DataSource": {
                "Rows": [
                    {
                        "rowType": "D",
                        "InventoryCode": "10001001",
                        "Inventory": "ABS树脂0215H",
                        "Specification": "",
                        "BaseUnit": "kg",
                        "WarehouseCode": "",
                        "Warehouse": "",
                        "BaseQuantity": "",
                        "canuseBaseQuantity": "",
                        "producefordispatchbasequantity": "1896.17",
                        "purchaseorderonwayBaseQuantity": "500",
                    },
                    {
                        "rowType": "T",  # Total/summary row should be ignored
                        "InventoryCode": "",
                        "BaseQuantity": "1000",
                    },
                ]
            },
        }

        rows = sync_current_stock(
            settings=self.settings,
            client=mock_client,
            timestamp="20261120_120000",
            inventory_class_map={"10001001": "原材料"},
        )

        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["InventoryCode"], "10001001")
        self.assertEqual(r["InventoryName"], "ABS树脂0215H")
        self.assertEqual(r["WarehouseCode"], "001")
        self.assertEqual(r["WarehouseName"], "原材库")
        self.assertEqual(r["InventoryClassName"], "原材料")
        self.assertEqual(r["UnitName"], "kg")
        self.assertEqual(r["ProduceDispatchQuantity"], 1896.17)
        self.assertEqual(r["ExistingQuantity"], 1896.17)
        self.assertEqual(r["AvailableQuantity"], 0.0)
        self.assertEqual(r["PurchaseOnWayQuantity"], 500.0)

    def test_export_current_stock_skips_when_empty(self):
        path = export_current_stock([], settings=self.settings, timestamp="20261120_120000")
        self.assertIsNone(path)

    def test_export_current_stock_writes_excel_when_data_present(self):
        rows = [
            {
                "WarehouseCode": "001",
                "WarehouseName": "原材库",
                "InventoryCode": "10001001",
                "InventoryName": "ABS树脂0215H",
                "InventoryClassName": "原材料",
                "Specification": "",
                "UnitName": "kg",
                "ExistingQuantity": 1896.17,
                "AvailableQuantity": 0.0,
                "ProduceDispatchQuantity": 1896.17,
                "PurchaseOnWayQuantity": 0.0,
            }
        ]
        path = export_current_stock(rows, settings=self.settings, timestamp="20261120_120000")
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "current_stock_20261120_120000.xlsx")


if __name__ == "__main__":
    unittest.main()
