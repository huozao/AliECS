from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"
sys.path.insert(0, str(WORKER_ROOT))


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


class WorkerImportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        _clear_app_modules()
        worker_root = str(WORKER_ROOT)
        sys.path[:] = [item for item in sys.path if item != worker_root]
        sys.path.insert(0, worker_root)

    def tearDown(self) -> None:
        _clear_app_modules()
        sys.path[:] = self._old_sys_path


class WeComSmartsheetPaginationTests(WorkerImportTestCase):
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


class ExternalRecordHashTests(WorkerImportTestCase):
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


class SourceUrlTests(WorkerImportTestCase):
    def test_build_smartsheet_open_url_prefers_source_url(self) -> None:
        from app.storage.postgres import build_smartsheet_open_url

        url = build_smartsheet_open_url("dcabc", "sheet1", "https://doc.weixin.qq.com/smartsheet/dcabc?tab=sheet1")

        self.assertEqual("https://doc.weixin.qq.com/smartsheet/dcabc?tab=sheet1", url)

    def test_build_smartsheet_open_url_falls_back_to_docid_and_sheet_id(self) -> None:
        from app.storage.postgres import build_smartsheet_open_url

        url = build_smartsheet_open_url("dcabc", "sheet1", "")

        self.assertEqual("https://doc.weixin.qq.com/smartsheet/dcabc?sheet_id=sheet1", url)


class SourceNameTests(WorkerImportTestCase):
    def test_compose_source_name_keeps_document_and_sheet_names_separate(self) -> None:
        from app.storage.postgres import compose_source_name

        source_name = compose_source_name("点检表", "点检计划")

        self.assertEqual("点检表 / 点检计划", source_name)

    def test_split_source_name_recovers_legacy_document_and_sheet_names(self) -> None:
        from app.storage.postgres import split_source_name

        names = split_source_name("点检表 / 点检明细")

        self.assertEqual({"document_name": "点检表", "sheet_name": "点检明细"}, names)


class EnvProfileTests(WorkerImportTestCase):
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

    def test_discover_profile_sources_uses_configured_smartsheet_name(self) -> None:
        from app.providers.wecom import discover_profile_sources

        with patch.dict(
            "os.environ",
            {
                "SMARTSHEET_COMPANY_B_ID": "dcFAKE_COMPANY_B_DOC_ID_000000000000000000000001",
                "SMARTSHEET_COMPANY_B_NAME": "点检表",
            },
            clear=True,
        ):
            sources = discover_profile_sources("COMPANY_B")
            self.assertEqual(1, len(sources))
            self.assertEqual("点检表", sources[0].source_name)


class FeishuProviderEnvTests(WorkerImportTestCase):
    def test_feishu_env_profiles_can_be_inferred_from_company_variables(self) -> None:
        from app.providers.feishu import env_profiles

        with patch.dict(
            "os.environ",
            {
                "FEISHU_COMPANY_A_APP_ID": "cli_a",
                "FEISHU_COMPANY_A_APP_SECRET": "secret-a",
                "FEISHU_COMPANY_B_APP_ID": "cli_b",
                "FEISHU_COMPANY_B_APP_SECRET": "secret-b",
            },
            clear=True,
        ):
            self.assertEqual(["COMPANY_A", "COMPANY_B"], env_profiles(""))

    def test_feishu_profile_discovers_bitable_source_from_env(self) -> None:
        from app.providers.feishu import credentials_for_profile, discover_profile_sources

        with patch.dict(
            "os.environ",
            {
                "FEISHU_COMPANY_A_APP_ID": "cli_a",
                "FEISHU_COMPANY_A_APP_SECRET": "secret-a",
                "FEISHU_COMPANY_A_APP_TOKEN": "bascn_test_token",
                "FEISHU_COMPANY_A_TABLE_ID": "tbl_test_table",
                "FEISHU_COMPANY_A_TABLE_NAME": "生产任务",
            },
            clear=True,
        ):
            credentials = credentials_for_profile("COMPANY_A")
            sources = discover_profile_sources("COMPANY_A")

        self.assertEqual(1, len(credentials))
        self.assertEqual("COMPANY_A", credentials[0].env_profile)
        self.assertEqual(1, len(sources))
        self.assertEqual("bascn_test_token", sources[0].app_token)
        self.assertEqual("tbl_test_table", sources[0].table_id)
        self.assertEqual("生产任务", sources[0].source_name)


class FeishuBitablePaginationTests(WorkerImportTestCase):
    def test_get_records_merges_two_pages(self) -> None:
        from app.providers.feishu import FeishuBitableClient

        pages = [
            {"code": 0, "data": {"items": [{"record_id": "r1"}], "has_more": True, "page_token": "next"}},
            {"code": 0, "data": {"items": [{"record_id": "r2"}], "has_more": False}},
        ]

        class FakeClient(FeishuBitableClient):
            def __init__(self) -> None:
                super().__init__("cli_a", "secret-a")
                self._tenant_token = "tenant-token"
                self.calls: list[dict] = []

            def _request_json(self, method: str, path: str, **kwargs: object) -> dict:
                self.calls.append({"method": method, "path": path, "kwargs": kwargs})
                return pages.pop(0)

        client = FakeClient()

        result = client.get_records("bascn_test_token", "tbl_test_table")

        self.assertEqual(["r1", "r2"], [x["record_id"] for x in result["records"]])
        self.assertEqual(2, result["page_count"])
        self.assertEqual(2, result["fetched_count"])
        self.assertEqual("next", client.calls[1]["kwargs"]["params"]["page_token"])

    def test_get_records_errors_when_has_more_without_page_token(self) -> None:
        from app.providers.feishu import FeishuBitableClient

        class FakeClient(FeishuBitableClient):
            def __init__(self) -> None:
                super().__init__("cli_a", "secret-a")
                self._tenant_token = "tenant-token"

            def _request_json(self, method: str, path: str, **kwargs: object) -> dict:
                return {"code": 0, "data": {"items": [{"record_id": "r1"}], "has_more": True}}

        with self.assertRaisesRegex(RuntimeError, "缺少 page_token"):
            FakeClient().get_records("bascn_test_token", "tbl_test_table")

    def test_get_records_errors_when_page_token_repeats(self) -> None:
        from app.providers.feishu import FeishuBitableClient

        class FakeClient(FeishuBitableClient):
            def __init__(self) -> None:
                super().__init__("cli_a", "secret-a")
                self._tenant_token = "tenant-token"

            def _request_json(self, method: str, path: str, **kwargs: object) -> dict:
                return {"code": 0, "data": {"items": [], "has_more": True, "page_token": "repeat"}}

        with self.assertRaisesRegex(RuntimeError, "page_token 重复"):
            FakeClient().get_records("bascn_test_token", "tbl_test_table")

    def test_redact_path_hides_bitable_app_token(self) -> None:
        from app.providers.feishu import FeishuBitableClient

        path = "/bitable/v1/apps/bascn_secret_token/tables/tbl_test_table/records"

        safe_path = FeishuBitableClient._redact_path(path)

        self.assertEqual("/bitable/v1/apps/***/tables/tbl_test_table/records", safe_path)
        self.assertNotIn("bascn_secret_token", safe_path)


class FeishuBitableErrorTests(WorkerImportTestCase):
    def test_request_json_http_error_includes_status_without_secrets(self) -> None:
        import requests

        from app.providers.feishu import FeishuBitableClient

        class FakeResponse:
            status_code = 403
            text = "app_secret=secret-a tenant_access_token=tenant-token"

            def raise_for_status(self) -> None:
                raise requests.HTTPError("403 Client Error", response=self)

            def json(self) -> dict:
                return {}

        class FakeSession:
            trust_env = False

            def request(self, *args: object, **kwargs: object) -> FakeResponse:
                return FakeResponse()

        client = FeishuBitableClient("cli_a", "secret-a")
        client.session = FakeSession()  # type: ignore[assignment]

        with self.assertRaises(RuntimeError) as raised:
            client._request_json("GET", "/bitable/v1/apps/app/tables/table/records")

        error = str(raised.exception)
        self.assertIn("/bitable/v1/apps/***/tables/table/records", error)
        self.assertNotIn("/bitable/v1/apps/app/tables/table/records", error)
        self.assertIn("http_status=403", error)
        self.assertNotIn("secret-a", error)
        self.assertNotIn("tenant-token", error)


if __name__ == "__main__":
    unittest.main()
