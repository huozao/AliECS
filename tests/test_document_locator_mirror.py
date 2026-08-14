from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "services" / "doc-sync-worker"


VALID_DOCID = "d" + "c" + ("m" * 86)


class FakeMirrorClient:
    def __init__(self) -> None:
        self.sheets = [
            {"sheet_id": "old-sheet", "properties": {"title": "企微A-最新结构"}},
        ]
        self.fields: dict[str, list[dict[str, Any]]] = {}
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.added_sheets: list[str] = []
        self.added_records: list[tuple[str, list[dict[str, Any]]]] = []
        self.updated_records: list[tuple[str, list[dict[str, Any]]]] = []

    def get_sheets(self, _docid: str) -> list[dict[str, Any]]:
        return list(self.sheets)

    def add_sheet(self, _docid: str, title: str, index: int | None = None) -> dict[str, Any]:
        del index
        sheet_id = "sheet-" + str(len(self.sheets) + 1)
        self.sheets.append({"sheet_id": sheet_id, "properties": {"title": title}})
        self.fields[sheet_id] = [
            {"field_id": "field-1", "field_title": "默认字段", "field_type": "FIELD_TYPE_TEXT"}
        ]
        self.added_sheets.append(title)
        return {}

    def get_fields(self, _docid: str, sheet_id: str) -> dict[str, Any]:
        return {"fields": list(self.fields.get(sheet_id, []))}

    def add_fields(self, _docid: str, sheet_id: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
        current = self.fields.setdefault(sheet_id, [])
        for field in fields:
            current.insert(
                1,
                {
                    "field_id": "field-" + str(len(current) + 1),
                    "field_title": field["field_title"],
                    "field_type": field.get("field_type", "FIELD_TYPE_TEXT"),
                }
            )
        return {}

    def update_fields(self, _docid: str, sheet_id: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
        by_id = {str(item["field_id"]): item for item in self.fields.get(sheet_id, [])}
        for field in fields:
            by_id[str(field["field_id"])].update(field)
        return {}

    def get_records(self, _docid: str, sheet_id: str) -> dict[str, Any]:
        return {"records": list(self.records.get(sheet_id, []))}

    @staticmethod
    def _accepted(values: dict[str, Any]) -> dict[str, Any]:
        """企微只认 [{"type": "text", "text": ...}] 形式的单元格；裸字符串会被静默丢成空值。"""
        return {title: cells for title, cells in values.items() if isinstance(cells, list)}

    def add_records(self, _docid: str, sheet_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        self.added_records.append((sheet_id, records))
        target = self.records.setdefault(sheet_id, [])
        for record in records:
            target.append(
                {"record_id": "record-" + str(len(target) + 1), "values": self._accepted(record["values"])}
            )
        return {}

    def update_records(self, _docid: str, sheet_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        self.updated_records.append((sheet_id, records))
        stored = {str(item.get("record_id")): item for item in self.records.get(sheet_id, [])}
        for record in records:
            target = stored.get(str(record.get("record_id")))
            if target is not None:
                target["values"] = self._accepted(record["values"])
        return {}


class FakeLocatorStore:
    def __init__(self) -> None:
        self.sources: list[dict[str, Any]] = []
        self.upserts: list[tuple[dict[str, Any], str]] = []
        self.jobs: list[dict[str, Any]] = []
        self.payloads: dict[int, dict[str, Any]] = {}
        self.finished: list[int] = []
        self.retried: list[tuple[int, str, int]] = []
        self.closed = 0

    def list_document_locator_sources(self, source_id: int | None = None) -> list[dict[str, Any]]:
        if source_id is None:
            return list(self.sources)
        return [item for item in self.sources if int(item["id"]) == source_id]

    def upsert_document_locator(self, locator: dict[str, Any], *, event_type: str, actor: str) -> dict[str, Any]:
        del actor
        self.upserts.append((locator, event_type))
        return {"id": len(self.upserts), "locator_version": 1, "changed": True, "created": True, "mirror_job_id": 1}

    def claim_document_locator_mirror_jobs(self, limit: int) -> list[dict[str, Any]]:
        return list(self.jobs[:limit])

    def get_document_locator_mirror_payload(self, locator_id: int, locator_version: int) -> dict[str, Any] | None:
        del locator_version
        return self.payloads.get(locator_id)

    def finish_document_locator_mirror_job(self, job_id: int) -> None:
        self.finished.append(job_id)

    def retry_document_locator_mirror_job(self, job_id: int, error: str, delay_seconds: int) -> None:
        self.retried.append((job_id, error, delay_seconds))

    def close(self) -> None:
        self.closed += 1


def source() -> dict[str, Any]:
    return {
        "id": 17,
        "provider": "wecom",
        "env_profile": "COMPANY_A",
        "external_doc_id": VALID_DOCID,
        "document_name": "live-name",
        "source_url": "https://example.invalid/share",
        "source_type": "smartsheet_doc",
        "status": "active",
        "sheet_count": 3,
        "last_sync_at": "2026-08-14T00:00:00Z",
    }


def mirror_payload() -> dict[str, Any]:
    return {
        "locator": {
            "id": 41,
            "provider": "wecom",
            "env_profile": "COMPANY_A",
            "api_doc_id": VALID_DOCID,
            "share_ref": "",
            "document_name": "live-name",
            "source_url": "https://example.invalid/share",
            "admin_userids": ["admin-one"],
            "credential_ref": "COMPANY_A#1",
            "source_kind": "registry",
            "lifecycle_status": "active",
            "syncability_status": "verified",
            "capabilities": {"read": "verified", "write": "unknown", "copy": "allowed"},
            "sheet_count": 3,
            "registered_at": "2026-08-13T00:00:00Z",
            "last_verified_at": "2026-08-14T00:00:00Z",
            "last_sync_at": "2026-08-14T00:00:00Z",
            "updated_at": "2026-08-14T00:00:00Z",
            "last_error_summary": "",
        },
        "event": {
            "event_type": "sync-success",
            "trigger_source": "sync-request:7",
            "changed_fields": ["last_sync_at"],
            "status_summary": {"syncability_status": "verified"},
            "created_at": "2026-08-14T00:00:00Z",
        },
    }


class DocumentLocatorMirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._old_path = list(sys.path)
        cls._old_app = {name: module for name, module in sys.modules.items() if name == "app" or name.startswith("app.")}
        for name in cls._old_app:
            sys.modules.pop(name, None)
        sys.path.insert(0, str(WORKER))
        cls.locator = importlib.import_module("app.pipelines.document_locator")
        cls.mirror = importlib.import_module("app.pipelines.document_locator_mirror")

    @classmethod
    def tearDownClass(cls) -> None:
        for name in tuple(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)
        sys.modules.update(cls._old_app)
        sys.path[:] = cls._old_path

    def test_reconcile_marks_synced_doc_read_verified_without_overwriting_private_metadata(self) -> None:
        store = FakeLocatorStore()
        store.sources = [source()]

        result = self.locator.reconcile_document_locators(store, trigger="full-sync")

        self.assertEqual({"seen": 1, "changed": 1, "failed": 0}, result)
        written = store.upserts[0][0]
        self.assertNotIn("admin_userids", written)
        self.assertNotIn("credential_ref", written)
        self.assertEqual("verified", written["capabilities"]["read"])
        self.assertEqual("unknown", written["capabilities"]["write"])

    def test_every_successful_request_reconciles_but_failure_does_not(self) -> None:
        store = FakeLocatorStore()
        store.sources = [source()]
        request = {"id": 7, "source_id": 17, "requested_by": "manual"}

        self.assertTrue(self.locator.record_locator_after_request(store, request, "success"))
        self.assertEqual("sync-request:7", store.upserts[0][1])
        self.assertFalse(self.locator.record_locator_after_request(store, request, "failed"))
        self.assertEqual(1, len(store.upserts))

    def test_permission_failure_is_written_and_not_erased_by_historical_last_sync(self) -> None:
        store = FakeLocatorStore()
        failed_source = source()
        failed_source.update(
            {
                "locator_updated_at": "2026-08-14T01:00:00Z",
                "locator_syncability_status": "permission-denied",
                "locator_last_error_code": "auth",
                "locator_last_error_summary": "企微权限验证失败",
            }
        )
        store.sources = [failed_source]

        self.assertTrue(
            self.locator.record_locator_failure(store, source_id=17, error=RuntimeError("403 forbidden"))
        )
        written = store.upserts[-1][0]
        self.assertEqual("permission-denied", written["syncability_status"])
        self.assertEqual("unavailable", written["capabilities"]["read"])
        self.assertNotIn("403", written["last_error_summary"])

        preserved = self.locator.locator_from_source(failed_source)
        self.assertEqual("permission-denied", preserved["syncability_status"])
        self.assertEqual("auth", preserved["last_error_code"])

    def test_live_read_success_clears_stale_permission_failure(self) -> None:
        store = FakeLocatorStore()
        failed_source = source()
        failed_source.update(
            {
                "locator_updated_at": "2026-08-14T01:00:00Z",
                "locator_syncability_status": "permission-denied",
                "locator_last_error_code": "auth",
                "locator_last_error_summary": "企微权限验证失败",
            }
        )
        store.sources = [failed_source]

        self.assertTrue(
            self.locator.record_locator_read_success(
                store, env_profile="COMPANY_A", api_doc_id=VALID_DOCID,
            )
        )

        restored, event_type = store.upserts[-1]
        self.assertEqual("permission-restored", event_type)
        self.assertEqual("verified", restored["syncability_status"])
        self.assertEqual("verified", restored["capabilities"]["read"])
        self.assertEqual("allowed", restored["capabilities"]["copy"])
        self.assertEqual("", restored["last_error_code"])
        self.assertEqual("", restored["last_error_summary"])

    def test_workbook_adds_only_two_authoritative_sheets_and_fixed_fields(self) -> None:
        client = FakeMirrorClient()

        workbook = self.mirror.ensure_locator_workbook(client, docid="backup-doc")

        self.assertEqual(
            {"文档定位档案", "定位档案变更历史"},
            set(workbook["sheets"]),
        )
        self.assertEqual({"文档定位档案", "定位档案变更历史"}, set(client.added_sheets))
        self.assertNotIn("企微A-最新结构", client.added_sheets)
        current_id = workbook["sheets"]["文档定位档案"]
        history_id = workbook["sheets"]["定位档案变更历史"]
        self.assertEqual(list(self.mirror.CURRENT_FIELDS), [item["field_title"] for item in client.fields[current_id]])
        self.assertEqual(list(self.mirror.EVENT_FIELDS), [item["field_title"] for item in client.fields[history_id]])

    def test_write_upserts_current_and_appends_each_event_once(self) -> None:
        client = FakeMirrorClient()
        workbook = self.mirror.ensure_locator_workbook(client, docid="backup-doc")

        first = self.mirror.write_locator_mirror(
            client,
            backup_docid="backup-doc",
            sheet_ids=workbook["sheets"],
            payload=mirror_payload(),
        )
        second = self.mirror.write_locator_mirror(
            client,
            backup_docid="backup-doc",
            sheet_ids=workbook["sheets"],
            payload=mirror_payload(),
        )

        self.assertEqual({"current_written": True, "event_added": True}, first)
        self.assertEqual({"current_written": True, "event_added": False}, second)
        all_titles = {key for _, batches in client.added_records for batch in batches for key in batch["values"]}
        self.assertNotIn("字段结构", all_titles)
        self.assertNotIn("工作表01名称", all_titles)
        # 每个单元格都必须是企微要求的 cell 数组，否则写进去的是空行。
        written = [
            (title, cells)
            for _, batches in client.added_records + client.updated_records
            for batch in batches
            for title, cells in batch["values"].items()
        ]
        self.assertTrue(written)
        for title, cells in written:
            self.assertIsInstance(cells, list, f"{title} 必须是 cell 数组")
            for cell in cells:
                self.assertEqual("text", cell.get("type"), f"{title} 单元格缺少 type=text")
                self.assertIsInstance(cell.get("text"), str, f"{title} 单元格缺少文本")
        current_rows = client.records[workbook["sheets"][self.mirror.CURRENT_SHEET]]
        self.assertEqual(1, len(current_rows), "同一份档案不应重复插行")

    def test_pending_jobs_finish_or_retry_without_leaking_error(self) -> None:
        store = FakeLocatorStore()
        store.jobs = [
            {"id": 81, "locator_id": 41, "locator_version": 1, "trigger": "sync-success", "attempt_count": 0}
        ]
        store.payloads[41] = mirror_payload()
        client = FakeMirrorClient()
        workbook = self.mirror.ensure_locator_workbook(client, docid="backup-doc")

        with mock.patch.object(self.mirror, "open_store", return_value=store), \
                mock.patch.object(self.mirror, "_workbook_client", return_value=(client, workbook)):
            self.assertEqual(0, self.mirror.run_pending_document_locator_mirror_jobs(limit=10, force=True))
        self.assertEqual([81], store.finished)

        failing = FakeLocatorStore()
        failing.jobs = list(store.jobs)
        failing.payloads[41] = mirror_payload()
        with mock.patch.object(self.mirror, "open_store", return_value=failing), \
                mock.patch.object(self.mirror, "_workbook_client", side_effect=RuntimeError("token=topsecret")):
            self.assertEqual(1, self.mirror.run_pending_document_locator_mirror_jobs(limit=10, force=True))
        self.assertEqual(81, failing.retried[0][0])
        self.assertNotIn("topsecret", failing.retried[0][1])


if __name__ == "__main__":
    unittest.main()
