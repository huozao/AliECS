from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"
sys.path.insert(0, str(WORKER_ROOT))


class WeComSmartsheetPaginationTests(unittest.TestCase):
    def test_get_records_stops_after_single_page_when_has_more_false(self) -> None:
        from app.providers.wecom import WeComSmartsheetClient

        class FakeClient(WeComSmartsheetClient):
            def __init__(self) -> None:
                super().__init__("corp", "secret")
                self.calls: list[dict] = []

            def _post(self, path: str, payload: dict) -> dict:
                self.calls.append({"path": path, "payload": payload})
                return {
                    "errcode": 0,
                    "has_more": False,
                    "next": "",
                    "records": [{"record_id": "r1"}],
                }

        client = FakeClient()

        result = client.get_records("doc1", "sheet1")

        self.assertEqual(1, result["fetched_count"])
        self.assertEqual(1, result["page_count"])
        self.assertEqual([{"record_id": "r1"}], result["records"])
        self.assertEqual(1, len(client.calls))
        self.assertNotIn("next", client.calls[0]["payload"])

    def test_get_records_follows_next_until_has_more_false(self) -> None:
        from app.providers.wecom import WeComSmartsheetClient

        pages = [
            {"errcode": 0, "has_more": True, "next": "cursor-1", "records": [{"record_id": "r1"}]},
            {"errcode": 0, "has_more": True, "next": "cursor-2", "records": [{"record_id": "r2"}]},
            {"errcode": 0, "has_more": False, "next": "", "records": [{"record_id": "r3"}]},
        ]

        class FakeClient(WeComSmartsheetClient):
            def __init__(self) -> None:
                super().__init__("corp", "secret")
                self.calls: list[dict] = []

            def _post(self, path: str, payload: dict) -> dict:
                self.calls.append({"path": path, "payload": dict(payload)})
                return pages.pop(0)

        client = FakeClient()

        result = client.get_records("doc1", "sheet1")

        self.assertEqual(3, result["fetched_count"])
        self.assertEqual(3, result["page_count"])
        self.assertEqual(["r1", "r2", "r3"], [item["record_id"] for item in result["records"]])
        self.assertEqual("cursor-1", client.calls[1]["payload"]["next"])
        self.assertEqual("cursor-2", client.calls[2]["payload"]["next"])


class ExternalRecordHashTests(unittest.TestCase):
    def test_same_record_hash_is_unchanged(self) -> None:
        from app.storage.postgres import build_record_snapshot, decide_record_upsert

        raw_record = {"record_id": "r1", "values": {"f1": [{"text": "alpha"}]}}
        snapshot = build_record_snapshot(raw_record, {"f1": "字段一"})

        decision = decide_record_upsert(snapshot.record_hash, snapshot)

        self.assertEqual("unchanged", decision.action)
        self.assertFalse(decision.should_write)

    def test_changed_record_hash_requests_update(self) -> None:
        from app.storage.postgres import build_record_snapshot, decide_record_upsert

        old_snapshot = build_record_snapshot({"record_id": "r1", "values": {"f1": [{"text": "alpha"}]}}, {"f1": "字段一"})
        new_snapshot = build_record_snapshot({"record_id": "r1", "values": {"f1": [{"text": "beta"}]}}, {"f1": "字段一"})

        decision = decide_record_upsert(old_snapshot.record_hash, new_snapshot)

        self.assertEqual("update", decision.action)
        self.assertTrue(decision.should_write)


class SourceUrlTests(unittest.TestCase):
    def test_build_smartsheet_open_url_prefers_source_url(self) -> None:
        from app.storage.postgres import build_smartsheet_open_url

        url = build_smartsheet_open_url("dcabc", "sheet1", "https://doc.weixin.qq.com/smartsheet/dcabc?tab=sheet1")

        self.assertEqual("https://doc.weixin.qq.com/smartsheet/dcabc?tab=sheet1", url)

    def test_build_smartsheet_open_url_falls_back_to_docid_and_sheet_id(self) -> None:
        from app.storage.postgres import build_smartsheet_open_url

        url = build_smartsheet_open_url("dcabc", "sheet1", "")

        self.assertEqual("https://doc.weixin.qq.com/smartsheet/dcabc?sheet_id=sheet1", url)


class EnvProfileTests(unittest.TestCase):
    def test_env_profiles_can_be_inferred_from_company_variables(self) -> None:
        from app.providers.wecom import env_profiles

        with patch.dict(
            "os.environ",
            {
                "WECOM_COMPANY_A_CORP_ID": "corp-a",
                "WECOM_COMPANY_A_APP_SECRET": "secret-a",
                "WEDOC_COMPANY_A_DOCID": "doc-a",
                "WECOM_COMPANY_B_CORP_ID": "corp-b",
                "WECOM_COMPANY_B_APP_SECRET": "secret-b",
            },
            clear=True,
        ):
            self.assertEqual(["COMPANY_A", "COMPANY_B"], env_profiles(""))

    def test_discover_profile_sources_ignores_placeholder_docids(self) -> None:
        from app.providers.wecom import discover_profile_sources

        with patch.dict(
            "os.environ",
            {
                "WEDOC_COMPANY_A_DOCID": "你的智能表格docid",
                "SMARTSHEET_COMPANY_A_ID": "dcFAKE_LOCAL_TEST_DOC_ID_000000000000000000000001",
            },
            clear=True,
        ):
            sources = discover_profile_sources("COMPANY_A")
            self.assertEqual(1, len(sources))
            self.assertTrue(sources[0].docid.startswith("dcFAKE_LOCAL_TEST"))


if __name__ == "__main__":
    unittest.main()
