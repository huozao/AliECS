import json
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from tplus_datahub.modules.bom.sync_bom import sync_bom
from tplus_datahub.modules.bom.transform_bom import transform_bom_rows


class FakeClient:
    def __init__(self):
        self.payloads = []

    def post(self, endpoint, payload):
        self.payloads.append((endpoint, payload.copy()))
        page_index = payload["param"]["PageIndex"]
        if page_index == 1:
            return {"Result": {"Rows": [{"id": 1, "material": {"code": "M001"}}]}}
        return {"Result": {"Rows": []}}


class BomSyncTests(unittest.TestCase):
    def test_sync_bom_pages_and_saves_raw_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
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
            client = FakeClient()

            rows = sync_bom(settings=settings, client=client, timestamp="20260601_153000")

            self.assertEqual(rows, [{"id": 1, "material": {"code": "M001"}}])
            self.assertEqual(len(client.payloads), 2)
            self.assertEqual(client.payloads[0][1]["param"]["PageIndex"], 1)
            self.assertEqual(client.payloads[0][1]["param"]["PageSize"], 1)
            self.assertEqual(client.payloads[1][1]["param"]["PageIndex"], 2)
            raw_file = Path(tmp) / "data" / "raw" / "bom" / "20260601_153000_page_1.json"
            self.assertTrue(raw_file.exists())
            self.assertEqual(json.loads(raw_file.read_text(encoding="utf-8"))["Result"]["Rows"][0]["id"], 1)

    def test_transform_bom_rows_flattens_nested_dicts(self):
        rows = [{"id": 1, "material": {"code": "M001", "name": "Steel"}}]

        result = transform_bom_rows(rows)

        self.assertEqual(result, [{"id": 1, "material.code": "M001", "material.name": "Steel"}])


if __name__ == "__main__":
    unittest.main()
