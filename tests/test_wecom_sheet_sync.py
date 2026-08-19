from __future__ import annotations

import sys
import unittest
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


class WeComSheetSyncTests(WorkerImportTestCase):
    def test_control_plane_doc_is_rescanned_when_modify_time_is_stale(self) -> None:
        from app.pipelines.sync_wecom_full import _sync_doc

        class FakeClient:
            def get_doc_base(self, docid: str) -> dict:
                return {"doc_name": "管理面板", "modify_time": "stale"}

            def get_sheets(self, docid: str) -> list[dict]:
                return [{"sheet_id": "config", "title": "企微AI助手配置"}]

            def get_fields(self, docid: str, sheet_id: str) -> dict:
                return {"fields": []}

            def get_records(self, docid: str, sheet_id: str) -> dict:
                return {"records": [], "page_count": 1}

        class FakeStore:
            def get_doc_modified(self, *_args) -> str:
                return "stale"

            def ensure_source(self, **_kwargs) -> int:
                return 10

            def replace_fields(self, _source_id: int, _fields: list[dict]) -> dict[str, str]:
                return {}

            def delete_missing_records(self, _source_id: int, _record_ids: list[str]) -> int:
                return 0

            def mark_source_synced(self, _source_id: int) -> None:
                pass

            def disable_missing_sheets(self, *_args) -> int:
                return 0

            def upsert_doc_source(self, **_kwargs) -> int:
                return 11

        counts = {"sheet_count": 0, "record_count": 0, "created_count": 0, "updated_count": 0, "error_count": 0}
        _sync_doc(
            FakeStore(),
            FakeClient(),
            profile="COMPANY_A",
            docid="management",
            fallback_name="fallback",
            source_url="",
            counts=counts,
            errors=[],
            skip_unchanged=True,
        )

        self.assertEqual(1, counts["sheet_count"])
        self.assertEqual(0, counts.get("skipped_doc_count", 0))

    def test_sheet_sync_removes_records_absent_from_latest_pull(self) -> None:
        from app.pipelines.sync_wecom_full import _sync_sheet_records
        from app.storage.postgres import UpsertDecision

        class FakeClient:
            def get_fields(self, docid: str, sheet_id: str) -> dict:
                return {"fields": [{"field_id": "peer", "field_title": "peer_id"}]}

            def get_records(self, docid: str, sheet_id: str) -> dict:
                return {
                    "records": [
                        {"record_id": "r1", "values": {"peer": [{"text": "wx-a"}]}},
                        {"record_id": "r2", "values": {"peer": [{"text": "wx-b"}]}},
                    ],
                    "page_count": 1,
                }

        class FakeStore:
            def __init__(self) -> None:
                self.deleted_missing: tuple[int, list[str]] | None = None
                self.synced_source_id: int | None = None

            def replace_fields(self, source_id: int, fields: list[dict]) -> dict[str, str]:
                return {"peer": "peer_id"}

            def upsert_record(self, source_id: int, snapshot) -> UpsertDecision:
                return UpsertDecision(action="unchanged", should_write=False)

            def delete_missing_records(self, source_id: int, external_record_ids: list[str]) -> int:
                self.deleted_missing = (source_id, list(external_record_ids))
                return 1

            def mark_source_synced(self, source_id: int) -> None:
                self.synced_source_id = source_id

        store = FakeStore()
        counts = {"sheet_count": 0, "record_count": 0, "created_count": 0, "updated_count": 0, "error_count": 0}

        _sync_sheet_records(store, FakeClient(), 839, "doc1", "sheet1", counts, "微信用户清单")

        self.assertEqual((839, ["r1", "r2"]), store.deleted_missing)
        self.assertEqual(839, store.synced_source_id)
        self.assertEqual(1, counts["deleted_count"])

    def test_doc_sync_disables_sources_for_sheets_missing_from_complete_pull(self) -> None:
        from app.pipelines.sync_wecom_full import _sync_doc
        from app.storage.postgres import UpsertDecision

        class FakeClient:
            def get_doc_base(self, docid: str) -> dict:
                return {"doc_name": "登记表-副本", "modify_time": "m2"}

            def get_sheets(self, docid: str) -> list[dict]:
                return [{"sheet_id": "sheet-live", "title": "配色&样品需求单"}]

            def get_fields(self, docid: str, sheet_id: str) -> dict:
                return {"fields": [{"field_id": "f1", "field_title": "字段"}]}

            def get_records(self, docid: str, sheet_id: str) -> dict:
                return {"records": [], "page_count": 1}

        class FakeStore:
            def __init__(self) -> None:
                self.disabled_missing: tuple[str, str, str, list[str]] | None = None

            def get_doc_modified(self, provider: str, env_profile: str, external_doc_id: str) -> str:
                return ""

            def ensure_source(self, **kwargs) -> int:
                return 10

            def replace_fields(self, source_id: int, fields: list[dict]) -> dict[str, str]:
                return {"f1": "字段"}

            def upsert_record(self, source_id: int, snapshot) -> UpsertDecision:
                return UpsertDecision(action="unchanged", should_write=False)

            def delete_missing_records(self, source_id: int, external_record_ids: list[str]) -> int:
                return 0

            def mark_source_synced(self, source_id: int) -> None:
                return None

            def disable_missing_sheets(
                self, provider: str, env_profile: str, external_doc_id: str, seen_sheet_ids: list[str]
            ) -> int:
                self.disabled_missing = (provider, env_profile, external_doc_id, list(seen_sheet_ids))
                return 1

            def upsert_doc_source(self, **kwargs) -> int:
                return 11

        store = FakeStore()
        counts = {"sheet_count": 0, "record_count": 0, "created_count": 0, "updated_count": 0, "error_count": 0}
        errors: list[dict] = []

        _sync_doc(
            store,
            FakeClient(),
            profile="COMPANY_B",
            docid="doc1",
            fallback_name="fallback",
            source_url="",
            counts=counts,
            errors=errors,
            skip_unchanged=False,
        )

        self.assertEqual(("wecom", "COMPANY_B", "doc1", ["sheet-live"]), store.disabled_missing)

    def test_doc_sync_does_not_disable_missing_sheets_for_empty_or_partial_pull(self) -> None:
        from app.pipelines.sync_wecom_full import _sync_doc

        class EmptyClient:
            def get_doc_base(self, docid: str) -> dict:
                return {"doc_name": "登记表-副本", "modify_time": "m2"}

            def get_sheets(self, docid: str) -> list[dict]:
                return []

        class FailingSheetClient(EmptyClient):
            def get_sheets(self, docid: str) -> list[dict]:
                return [{"sheet_id": "sheet-live", "title": "配色&样品需求单"}]

            def get_fields(self, docid: str, sheet_id: str) -> dict:
                raise RuntimeError("field api failed")

        class FakeStore:
            def __init__(self) -> None:
                self.disable_calls = 0

            def get_doc_modified(self, provider: str, env_profile: str, external_doc_id: str) -> str:
                return ""

            def ensure_source(self, **kwargs) -> int:
                return 10

            def disable_missing_sheets(self, *args) -> int:
                self.disable_calls += 1
                return 0

            def upsert_doc_source(self, **kwargs) -> int:
                return 11

        for client in (EmptyClient(), FailingSheetClient()):
            store = FakeStore()
            counts = {"sheet_count": 0, "record_count": 0, "created_count": 0, "updated_count": 0, "error_count": 0}
            _sync_doc(
                store,
                client,
                profile="COMPANY_B",
                docid="doc1",
                fallback_name="fallback",
                source_url="",
                counts=counts,
                errors=[],
                skip_unchanged=False,
            )
            self.assertEqual(0, store.disable_calls)


class AddSheetIndexTestCase(unittest.TestCase):
    """`add_sheet` 的可选 index 契约。原属 test_wecom_structure_backup.py，
    结构备份 2026-08-19 下线后迁来，测的是 provider 而非那条流水线。"""

    def setUp(self) -> None:
        _clear_app_modules()

    def test_add_sheet_passes_optional_index(self) -> None:
        from app.providers.wecom import WeComSmartsheetClient

        client = WeComSmartsheetClient("corp", "secret")
        calls = []
        client._post = lambda path, payload: calls.append((path, payload)) or {}
        client.add_sheet("DOC1", "表A")
        client.add_sheet("DOC1", "表B", 2)
        self.assertEqual({"docid": "DOC1", "properties": {"title": "表A"}}, calls[0][1])
        self.assertEqual({"docid": "DOC1", "properties": {"title": "表B", "index": 2}}, calls[1][1])


if __name__ == "__main__":
    unittest.main()
