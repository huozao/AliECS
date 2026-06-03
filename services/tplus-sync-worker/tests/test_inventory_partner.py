import json
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from tplus_datahub.modules.inventory.sync_stock import sync_inventory
from tplus_datahub.modules.partner.sync_partner import sync_partner


class FakeQueryPageClient:
    def __init__(self):
        self.calls = []

    def post(self, endpoint, payload):
        self.calls.append((endpoint, payload.copy()))
        page_index = payload["param"]["PageIndex"]
        if page_index == 1:
            return {"Data": [{"code": "001", "name": "first"}]}
        return {"Data": []}


class FakeDisabledSplitClient:
    def __init__(self):
        self.calls = []

    def post(self, endpoint, payload):
        self.calls.append((endpoint, payload.copy()))
        page_index = payload["param"]["PageIndex"]
        disabled = payload["param"].get("Disabled")
        if page_index == 1 and disabled == "0":
            return {"Data": [{"Code": "ACTIVE", "Name": "active", "Disabled": None}]}
        if page_index == 1 and disabled == "1":
            return {"Data": [{"Code": "DISABLED", "Name": "disabled", "Disabled": None}]}
        return {"Data": []}


class InventoryPartnerSyncTests(unittest.TestCase):
    def _settings(self, tmp):
        return Settings(
            base_url="https://openapi.example.com",
            app_key="app-key",
            app_secret="app-secret",
            open_token="open-token",
            default_page_size=1,
            timeout_connect=5,
            timeout_read=30,
            output_dir=str(Path(tmp) / "output"),
            data_dir=str(Path(tmp) / "data"),
        )

    def test_sync_inventory_pages_and_saves_raw_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeQueryPageClient()

            rows = sync_inventory(
                settings=self._settings(tmp),
                client=client,
                timestamp="20260602_200000",
                query_params={},
            )

            self.assertEqual(rows, [{"code": "001", "name": "first"}])
            self.assertEqual(client.calls[0][0], "/tplus/api/v2/inventory/QueryPage")
            self.assertEqual(client.calls[0][1]["param"]["PageSize"], 1)
            raw_file = Path(tmp) / "data" / "raw" / "inventory" / "20260602_200000_page_1.json"
            self.assertTrue(raw_file.exists())
            self.assertEqual(json.loads(raw_file.read_text(encoding="utf-8"))["Data"][0]["code"], "001")

    def test_sync_inventory_fetches_enabled_and_disabled_pages_and_marks_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeDisabledSplitClient()

            rows = sync_inventory(settings=self._settings(tmp), client=client, timestamp="20260604_010000")

            self.assertEqual(
                rows,
                [
                    {"Code": "ACTIVE", "Name": "active", "Disabled": "False"},
                    {"Code": "DISABLED", "Name": "disabled", "Disabled": "True"},
                ],
            )
            self.assertEqual(len(client.calls), 4)
            self.assertEqual(client.calls[0][1]["param"]["Disabled"], "0")
            self.assertEqual(client.calls[2][1]["param"]["Disabled"], "1")
            enabled_raw = Path(tmp) / "data" / "raw" / "inventory" / "20260604_010000_enabled_page_1.json"
            disabled_raw = Path(tmp) / "data" / "raw" / "inventory" / "20260604_010000_disabled_page_1.json"
            self.assertTrue(enabled_raw.exists())
            self.assertTrue(disabled_raw.exists())

    def test_sync_partner_pages_and_saves_raw_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeQueryPageClient()

            rows = sync_partner(
                settings=self._settings(tmp),
                client=client,
                timestamp="20260602_200100",
                query_params={},
            )

            self.assertEqual(rows, [{"code": "001", "name": "first"}])
            self.assertEqual(client.calls[0][0], "/tplus/api/v2/partner/QueryPage")
            raw_file = Path(tmp) / "data" / "raw" / "partner" / "20260602_200100_page_1.json"
            self.assertTrue(raw_file.exists())

    def test_sync_partner_fetches_enabled_and_disabled_pages_and_marks_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeDisabledSplitClient()

            rows = sync_partner(settings=self._settings(tmp), client=client, timestamp="20260604_010100")

            self.assertEqual(
                rows,
                [
                    {"Code": "ACTIVE", "Name": "active", "Disabled": "False"},
                    {"Code": "DISABLED", "Name": "disabled", "Disabled": "True"},
                ],
            )
            self.assertEqual(len(client.calls), 4)
            self.assertEqual(client.calls[0][0], "/tplus/api/v2/partner/QueryPage")
            self.assertEqual(client.calls[0][1]["param"]["Disabled"], "0")
            self.assertEqual(client.calls[2][1]["param"]["Disabled"], "1")
            enabled_raw = Path(tmp) / "data" / "raw" / "partner" / "20260604_010100_enabled_page_1.json"
            disabled_raw = Path(tmp) / "data" / "raw" / "partner" / "20260604_010100_disabled_page_1.json"
            self.assertTrue(enabled_raw.exists())
            self.assertTrue(disabled_raw.exists())


if __name__ == "__main__":
    unittest.main()
