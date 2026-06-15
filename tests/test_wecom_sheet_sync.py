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


if __name__ == "__main__":
    unittest.main()
