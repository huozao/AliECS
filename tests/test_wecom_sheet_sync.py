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


class SkipAndRetryContractTests(WorkerImportTestCase):
    """整簿跳过要留痕；半途失败下一轮必须重试。"""

    class _SkipClient:
        def get_doc_base(self, docid: str) -> dict:
            return {"doc_name": "点餐表", "modify_time": "same"}

        def get_sheets(self, docid: str) -> list[dict]:
            return [{"sheet_id": "s1", "title": "表1"}]

    class _SkipStore:
        def __init__(self) -> None:
            self.started: list[dict] = []
            self.finished: list[dict] = []
            self.listed: tuple = ()
            self.sync_jobs = self

        def get_doc_modified(self, *_args) -> str:
            return "same"

        def list_active_sheet_sources(self, provider: str, env_profile: str, docid: str) -> list[dict]:
            self.listed = (provider, env_profile, docid)
            return [
                {"source_id": 7, "source_name": "点餐表/表1", "sheet_name": "表1"},
                {"source_id": 8, "source_name": "点餐表/表2", "sheet_name": "表2"},
            ]

        # --- SyncJobPlatformWriter 的最小替身 ---
        def start_run(self, **kwargs) -> int:
            self.started.append(kwargs)
            return 100 + len(self.started)

        def upsert_step(self, *_args, **_kwargs) -> None:
            return None

        def finish_run(self, run_id: int, **kwargs) -> None:
            self.finished.append({"run_id": run_id, **kwargs})

    def test_whole_workbook_skip_still_records_one_run_per_job(self) -> None:
        from app.pipelines.sync_wecom_full import _sync_doc

        store = self._SkipStore()
        counts = {"sheet_count": 0, "record_count": 0, "created_count": 0, "updated_count": 0, "error_count": 0}
        _sync_doc(
            store,
            self._SkipClient(),
            profile="COMPANY_A",
            docid="doc1",
            fallback_name="fallback",
            source_url="",
            counts=counts,
            errors=[],
            skip_unchanged=True,
            legacy_run_id=55,
        )

        self.assertEqual(1, counts["skipped_doc_count"])
        self.assertEqual(("wecom", "COMPANY_A", "doc1"), store.listed)
        self.assertEqual(["wecom.doc.7", "wecom.doc.8"], [item["job_key"] for item in store.started])
        self.assertEqual(["skipped", "skipped"], [item["status"] for item in store.finished])

    def test_partial_failure_clears_modify_time_so_next_round_retries(self) -> None:
        """半途失败不得登记 modify_time，否则下一轮整簿跳过、永不重试。

        源码注释一直这么写，但 modify_time 曾是无条件登记的：
        wecom.doc.2 产量统计 因此从 2026-08-13 失败起被每晚跳过，定时任务从未重试过。
        """
        from app.pipelines.sync_wecom_full import _sync_doc

        class FailingClient:
            def get_doc_base(self, docid: str) -> dict:
                return {"doc_name": "产量统计", "modify_time": "m-new"}

            def get_sheets(self, docid: str) -> list[dict]:
                return [{"sheet_id": "s1", "title": "公开的生产记录表"}]

            def get_fields(self, docid: str, sheet_id: str) -> dict:
                raise RuntimeError("field api failed")

        class RecordingStore:
            def __init__(self) -> None:
                self.doc_source_kwargs: dict = {}

            def get_doc_modified(self, *_args) -> str:
                return ""

            def ensure_source(self, **_kwargs) -> int:
                return 2

            def disable_missing_sheets(self, *_args) -> int:
                return 0

            def upsert_doc_source(self, **kwargs) -> int:
                self.doc_source_kwargs = kwargs
                return 10

        store = RecordingStore()
        counts = {"sheet_count": 0, "record_count": 0, "created_count": 0, "updated_count": 0, "error_count": 0}
        _sync_doc(
            store,
            FailingClient(),
            profile="COMPANY_A",
            docid="doc1",
            fallback_name="fallback",
            source_url="",
            counts=counts,
            errors=[],
            skip_unchanged=True,
        )

        self.assertEqual(1, counts["error_count"])
        self.assertEqual("", store.doc_source_kwargs["external_modified_at"])

    def test_successful_document_still_registers_modify_time(self) -> None:
        from app.pipelines.sync_wecom_full import _sync_doc
        from app.storage.postgres import UpsertDecision

        class OkClient:
            def get_doc_base(self, docid: str) -> dict:
                return {"doc_name": "点餐表", "modify_time": "m-new"}

            def get_sheets(self, docid: str) -> list[dict]:
                return [{"sheet_id": "s1", "title": "表1"}]

            def get_fields(self, docid: str, sheet_id: str) -> dict:
                return {"fields": []}

            def get_records(self, docid: str, sheet_id: str) -> dict:
                return {"records": [], "page_count": 1, "unreadable_count": 0}

        class RecordingStore:
            def __init__(self) -> None:
                self.doc_source_kwargs: dict = {}

            def get_doc_modified(self, *_args) -> str:
                return ""

            def ensure_source(self, **_kwargs) -> int:
                return 7

            def replace_fields(self, *_args) -> dict:
                return {}

            def upsert_record(self, *_args):
                return UpsertDecision(action="unchanged", should_write=False)

            def delete_missing_records(self, *_args) -> int:
                return 0

            def mark_source_synced(self, *_args) -> None:
                return None

            def disable_missing_sheets(self, *_args) -> int:
                return 0

            def upsert_doc_source(self, **kwargs) -> int:
                self.doc_source_kwargs = kwargs
                return 10

        store = RecordingStore()
        counts = {"sheet_count": 0, "record_count": 0, "created_count": 0, "updated_count": 0, "error_count": 0}
        _sync_doc(
            store,
            OkClient(),
            profile="COMPANY_A",
            docid="doc1",
            fallback_name="fallback",
            source_url="",
            counts=counts,
            errors=[],
            skip_unchanged=True,
        )

        self.assertEqual(0, counts["error_count"])
        self.assertEqual("m-new", store.doc_source_kwargs["external_modified_at"])

    def test_unreadable_records_suspend_the_delete_comparison(self) -> None:
        """读不出来的记录不能被推断成上游已删，否则会把库里仍然有效的行抹掉。"""
        from app.pipelines.sync_wecom_full import _sync_sheet_records
        from app.storage.postgres import UpsertDecision

        class PartialClient:
            def get_fields(self, docid: str, sheet_id: str) -> dict:
                return {"fields": []}

            def get_records(self, docid: str, sheet_id: str) -> dict:
                return {
                    "records": [{"record_id": "r1", "values": {}}],
                    "page_count": 3,
                    "unreadable_count": 3,
                    "unreadable_offsets": [85, 86, 97],
                }

        class FakeStore:
            def __init__(self) -> None:
                self.delete_calls = 0

            def replace_fields(self, *_args) -> dict:
                return {}

            def upsert_record(self, *_args):
                return UpsertDecision(action="unchanged", should_write=False)

            def delete_missing_records(self, *_args) -> int:
                self.delete_calls += 1
                return 0

            def mark_source_synced(self, *_args) -> None:
                return None

        store = FakeStore()
        counts = {"sheet_count": 0, "record_count": 0, "created_count": 0, "updated_count": 0, "error_count": 0}
        _sync_sheet_records(store, PartialClient(), 2, "doc1", "s1", counts, "公开的生产记录表")

        self.assertEqual(0, store.delete_calls)
        self.assertEqual(3, counts["unreadable_record_count"])
        self.assertEqual(0, counts["error_count"])


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
