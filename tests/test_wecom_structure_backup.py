from __future__ import annotations

import sys
import unittest
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKER_ROOT = ROOT / "services" / "doc-sync-worker"


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


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.calls.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.cursor_obj = FakeCursor(rows)
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1


class StructureBackupStoreTests(WorkerImportTestCase):
    def test_store_groups_feishu_tables_by_app_token(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore

        rows = [
            (
                88,
                "COMPANY_A",
                "bascn-app",
                "飞书会话管理台",
                "https://example.feishu.cn/base/bascn-app",
                "bitable_app",
                "active",
                "2026-06-22 01:00:00+00",
                101,
                "tbl-one",
                "会话索引表",
                "bitable_table",
                "2026-06-22 01:00:00+00",
                501,
                "fld-name",
                "会话名称",
                "1",
                {"field_id": "fld-name", "field_name": "会话名称", "type": 1},
            ),
            (
                88,
                "COMPANY_A",
                "bascn-app",
                "飞书会话管理台",
                "https://example.feishu.cn/base/bascn-app",
                "bitable_app",
                "active",
                "2026-06-22 01:00:00+00",
                102,
                "tbl-two",
                "消息日志表",
                "bitable_table",
                "2026-06-22 01:00:00+00",
                502,
                "fld-message",
                "消息内容",
                "1",
                {"field_id": "fld-message", "field_name": "消息内容", "type": 1},
            ),
        ]
        conn = FakeConn(rows)

        documents = PostgresDocSyncStore(conn).list_feishu_document_structures(source_id=88)

        self.assertEqual(1, len(documents))
        self.assertEqual("feishu", documents[0]["provider"])
        self.assertEqual("bascn-app", documents[0]["external_doc_id"])
        self.assertEqual(["tbl-one", "tbl-two"], [sheet["external_sheet_id"] for sheet in documents[0]["sheets"]])
        self.assertNotIn("external_records", conn.cursor_obj.calls[-1][0])

    def test_store_upserts_generic_structure_document_anchor(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore

        conn = FakeConn([(99,)])
        source_id = PostgresDocSyncStore(conn).upsert_structure_document(
            provider="wecom",
            env_profile="COMPANY_A",
            source_type="structure_backup_doc",
            external_doc_id="dc-backup",
            document_name="企微智能表格结构备份",
            source_url="https://doc.weixin.qq.com/smartsheet/dc-backup",
        )

        sql, params = conn.cursor_obj.calls[-1]
        self.assertEqual(99, source_id)
        self.assertIn("source_type = EXCLUDED.source_type", sql)
        self.assertIn("RETURNING id", sql)
        self.assertEqual(
            (
                "wecom",
                "COMPANY_A",
                "企微智能表格结构备份",
                "structure_backup_doc",
                "dc-backup",
                "https://doc.weixin.qq.com/smartsheet/dc-backup",
                "企微智能表格结构备份",
            ),
            params,
        )

    def test_store_deactivates_stale_structure_sheet_ids(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore

        conn = FakeConn()
        PostgresDocSyncStore(conn).deactivate_missing_structure_sheets(
            provider="wecom",
            env_profile="COMPANY_A",
            external_doc_id="dc-backup",
            active_sheet_ids=["sheet-a", "sheet-b", "sheet-f", "sheet-h"],
        )

        sql, params = conn.cursor_obj.calls[-1]
        self.assertIn("status = 'inactive'", sql)
        self.assertIn("external_sheet_id <> ALL", sql)
        self.assertEqual(
            ("wecom", "COMPANY_A", "dc-backup", ["sheet-a", "sheet-b", "sheet-f", "sheet-h"]),
            params,
        )

    def test_migration_declares_durable_idempotent_job_queue(self) -> None:
        migration = ROOT / "db" / "migrations" / "0017_wecom_structure_backup.sql"
        sql = migration.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS wecom_structure_backup_jobs", sql)
        self.assertIn("event_key TEXT NOT NULL UNIQUE", sql)
        self.assertIn("next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW()", sql)
        self.assertIn("idx_wecom_structure_backup_jobs_pending", sql)

    def test_store_enqueues_job_idempotently(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore

        conn = FakeConn()
        store = PostgresDocSyncStore(conn)

        store.enqueue_structure_backup_job(
            source_id=42,
            trigger="daily",
            event_key="daily:2026-06-21:COMPANY_A:doc-1",
        )

        sql, params = conn.cursor_obj.calls[-1]
        self.assertIn("ON CONFLICT(event_key) DO NOTHING", sql)
        self.assertEqual((42, "daily:2026-06-21:COMPANY_A:doc-1", "daily"), params)
        self.assertEqual(1, conn.commits)

    def test_store_lists_ready_pending_jobs(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore

        rows = [(7, 42, "copy-auto:doc-1", "copy-auto", 2, "2026-06-21 10:00:00+00")]
        conn = FakeConn(rows)
        jobs = PostgresDocSyncStore(conn).pending_structure_backup_jobs(limit=10)

        self.assertEqual(
            [
                {
                    "id": 7,
                    "source_id": 42,
                    "event_key": "copy-auto:doc-1",
                    "trigger": "copy-auto",
                    "attempt_count": 2,
                    "created_at": "2026-06-21 10:00:00+00",
                }
            ],
            jobs,
        )
        sql, params = conn.cursor_obj.calls[-1]
        self.assertIn("next_attempt_at <= NOW()", sql)
        self.assertEqual((10,), params)

    def test_store_claims_jobs_atomically_with_skip_locked(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore

        rows = [(7, 42, "copy-auto:doc-1", "copy-auto", 2, "2026-06-21 10:00:00+00")]
        conn = FakeConn(rows)
        jobs = PostgresDocSyncStore(conn).claim_structure_backup_jobs(limit=10)

        sql, params = conn.cursor_obj.calls[-1]
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("UPDATE wecom_structure_backup_jobs", sql)
        self.assertIn("RETURNING", sql)
        self.assertEqual((10,), params)
        self.assertEqual(7, jobs[0]["id"])
        self.assertEqual(1, conn.commits)

    def test_store_transitions_running_retry_and_success(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore

        conn = FakeConn()
        store = PostgresDocSyncStore(conn)

        store.mark_structure_backup_job_running(7)
        store.retry_structure_backup_job(7, "temporary failure", delay_seconds=60)
        store.finish_structure_backup_job(7)

        calls = conn.cursor_obj.calls
        self.assertIn("status = 'running'", calls[0][0])
        self.assertEqual((7,), calls[0][1])
        self.assertIn("attempt_count = attempt_count + 1", calls[1][0])
        self.assertIn("next_attempt_at = NOW() + (%s * INTERVAL '1 second')", calls[1][0])
        self.assertEqual(("temporary failure", 60, 7), calls[1][1])
        self.assertIn("status = 'success'", calls[2][0])
        self.assertEqual((7,), calls[2][1])
        self.assertEqual(3, conn.commits)

    def test_store_groups_document_sheets_and_fields_without_business_records(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore

        rows = [
            (
                42,
                "COMPANY_A",
                "dc-1",
                "产品结构",
                "https://doc.weixin.qq.com/smartsheet/dc-1",
                "smartsheet_doc",
                "active",
                "modify-1",
                "2026-06-21 10:00:00+00",
                "2026-06-21 09:00:00+00",
                100,
                "sheet-a",
                "主表",
                "smartsheet_sheet",
                "2026-06-21 10:00:00+00",
                501,
                "field-a",
                "名称",
                "FIELD_TYPE_TEXT",
                {"field_id": "field-a", "field_title": "名称", "field_type": "FIELD_TYPE_TEXT"},
            ),
            (
                42,
                "COMPANY_A",
                "dc-1",
                "产品结构",
                "https://doc.weixin.qq.com/smartsheet/dc-1",
                "smartsheet_doc",
                "active",
                "modify-1",
                "2026-06-21 10:00:00+00",
                "2026-06-21 09:00:00+00",
                100,
                "sheet-a",
                "主表",
                "smartsheet_sheet",
                "2026-06-21 10:00:00+00",
                502,
                "field-b",
                "状态",
                "FIELD_TYPE_SINGLE_SELECT",
                {"field_id": "field-b", "field_title": "状态", "field_type": "FIELD_TYPE_SINGLE_SELECT"},
            ),
        ]
        conn = FakeConn(rows)

        documents = PostgresDocSyncStore(conn).list_wecom_document_structures(source_id=42)

        self.assertEqual(1, len(documents))
        self.assertEqual("dc-1", documents[0]["external_doc_id"])
        self.assertEqual("sheet-a", documents[0]["sheets"][0]["external_sheet_id"])
        self.assertEqual(2, len(documents[0]["sheets"][0]["fields"]))
        sql, params = conn.cursor_obj.calls[-1]
        self.assertNotIn("external_records", sql)
        self.assertEqual((42, 42), params)

    def test_replace_fields_removes_fields_absent_from_latest_structure(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore

        conn = FakeConn()
        PostgresDocSyncStore(conn).replace_fields(
            100,
            [{"field_id": "field-a", "field_title": "名称", "field_type": "FIELD_TYPE_TEXT"}],
        )

        delete_sql, delete_params = conn.cursor_obj.calls[-1]
        self.assertIn("DELETE FROM external_fields", delete_sql)
        self.assertIn("external_field_id <> ALL", delete_sql)
        self.assertEqual((100, ["field-a"]), delete_params)

    def test_replace_fields_uses_feishu_field_name_as_title(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore

        conn = FakeConn()
        titles = PostgresDocSyncStore(conn).replace_fields(
            100,
            [{"field_id": "fld-name", "field_name": "会话名称", "type": 1}],
        )

        insert_params = conn.cursor_obj.calls[0][1]
        self.assertEqual({"fld-name": "会话名称"}, titles)
        self.assertEqual("会话名称", insert_params[2])


class StructureSnapshotTests(WorkerImportTestCase):
    def _source(self) -> dict[str, Any]:
        return {
            "id": 42,
            "env_profile": "COMPANY_A",
            "external_doc_id": "dc-1",
            "document_name": "产品结构",
            "source_url": "https://doc.weixin.qq.com/smartsheet/dc-1",
            "source_type": "smartsheet_doc",
            "status": "active",
            "external_modified_at": "modify-1",
            "last_sync_at": "2026-06-21 10:00:00+00",
            "copy_requested_at": None,
        }

    def _sheets(self) -> list[dict[str, Any]]:
        return [
            {
                "source_id": 101,
                "external_sheet_id": "sheet-a",
                "sheet_name": "主表",
                "source_type": "smartsheet_sheet",
                "last_sync_at": "2026-06-21 10:00:00+00",
                "fields": [
                    {
                        "order": 1,
                        "external_field_id": "field-a",
                        "field_title": "名称",
                        "field_type": "FIELD_TYPE_TEXT",
                        "raw_json": {
                            "field_id": "field-a",
                            "field_title": "名称",
                            "field_type": "FIELD_TYPE_TEXT",
                            "is_primary": True,
                            "is_synced": True,
                        },
                    },
                    {
                        "order": 2,
                        "external_field_id": "field-b",
                        "field_title": "状态",
                        "field_type": "FIELD_TYPE_SINGLE_SELECT",
                        "raw_json": {
                            "field_id": "field-b",
                            "field_name": "状态",
                            "type": "FIELD_TYPE_SINGLE_SELECT",
                            "property_single_select": {"options": [{"id": "o1", "text": "启用"}]},
                        },
                    },
                ],
            },
            {
                "source_id": 102,
                "external_sheet_id": "sheet-b",
                "sheet_name": "统计",
                "source_type": "smartsheet_sheet",
                "last_sync_at": "2026-06-21 10:00:00+00",
                "fields": [],
            },
        ]

    def test_build_document_snapshot_is_stable_and_excludes_volatile_field_metadata(self) -> None:
        from app.pipelines.wecom_structure_backup import build_document_snapshot

        sheets = self._sheets()
        first = build_document_snapshot(self._source(), sheets, max_sheets=20)
        sheets[0]["fields"][0]["raw_json"]["is_synced"] = False
        second = build_document_snapshot(self._source(), list(reversed(sheets)), max_sheets=20)

        self.assertEqual("COMPANY_A:dc-1", first.unique_key)
        self.assertEqual(first.structure_hash, second.structure_hash)
        self.assertEqual("sheet-a", first.values["工作表01编码"])
        self.assertEqual("主表", first.values["工作表01名称"])
        fields = json.loads(first.values["工作表01字段结构"])
        self.assertEqual(
            {
                "id": "field-a",
                "name": "名称",
                "type": "FIELD_TYPE_TEXT",
                "order": 1,
                "config": {"is_primary": True},
            },
            fields[0],
        )
        self.assertEqual(
            {"property_single_select": {"options": [{"id": "o1", "text": "启用"}]}},
            fields[1]["config"],
        )
        serialized = json.dumps(first.values, ensure_ascii=False)
        self.assertNotIn("external_records", serialized)
        self.assertNotIn("is_synced", serialized)
        self.assertNotIn("记录数量", serialized)

    def test_build_document_snapshot_hash_changes_with_structure(self) -> None:
        from app.pipelines.wecom_structure_backup import build_document_snapshot

        before = build_document_snapshot(self._source(), self._sheets(), max_sheets=20)
        changed = self._sheets()
        changed[0]["fields"][0]["field_title"] = "产品名称"
        changed[0]["fields"][0]["raw_json"]["field_title"] = "产品名称"
        after = build_document_snapshot(self._source(), changed, max_sheets=20)

        self.assertNotEqual(before.structure_hash, after.structure_hash)

    def test_build_document_snapshot_rejects_more_than_configured_sheets(self) -> None:
        from app.pipelines.wecom_structure_backup import StructureBackupError, build_document_snapshot

        sheets = [
            {
                "source_id": index,
                "external_sheet_id": f"sheet-{index}",
                "sheet_name": f"表{index}",
                "fields": [],
            }
            for index in range(21)
        ]

        with self.assertRaisesRegex(StructureBackupError, "21.*20"):
            build_document_snapshot(self._source(), sheets, max_sheets=20)

    def test_company_b_keeps_exact_source_docid_and_url(self) -> None:
        from app.pipelines.wecom_structure_backup import build_document_snapshot

        source = self._source()
        source["env_profile"] = "COMPANY_B"
        snapshot = build_document_snapshot(source, self._sheets(), max_sheets=20)

        self.assertEqual(
            "https://doc.weixin.qq.com/smartsheet/dc-1",
            snapshot.values["来源链接"],
        )
        self.assertEqual("dc-1", snapshot.values["docid"])
        self.assertEqual("COMPANY_B:dc-1", snapshot.unique_key)

    def test_build_feishu_snapshot_targets_feishu_sheet_and_uses_app_token(self) -> None:
        from app.pipelines.wecom_structure_backup import build_document_snapshot

        source = {
            "id": 88,
            "provider": "feishu",
            "env_profile": "COMPANY_A",
            "external_doc_id": "bascn-app",
            "document_name": "飞书会话管理台",
            "source_url": "https://example.feishu.cn/base/bascn-app",
            "source_type": "bitable_app",
            "status": "active",
            "last_sync_at": "2026-06-22 01:00:00+00",
        }
        sheets = [
            {
                "source_id": 101,
                "external_sheet_id": "tbl-one",
                "sheet_name": "会话索引表",
                "source_type": "bitable_table",
                "fields": [
                    {
                        "order": 1,
                        "external_field_id": "fld-name",
                        "field_title": "会话名称",
                        "field_type": "1",
                        "raw_json": {"field_id": "fld-name", "field_name": "会话名称", "type": 1},
                    }
                ],
            }
        ]

        snapshot = build_document_snapshot(source, sheets, max_sheets=20)

        self.assertEqual("飞书", snapshot.platform)
        self.assertEqual("飞书-最新结构", snapshot.target_sheet_title)
        self.assertEqual("FEISHU:COMPANY_A:bascn-app", snapshot.unique_key)
        self.assertEqual("bascn-app", snapshot.values["文档定位ID"])
        self.assertEqual("", snapshot.values["docid"])
        self.assertEqual("tbl-one", snapshot.values["工作表01编码"])


class StructureBackupWeComApiTests(WorkerImportTestCase):
    def test_backup_schema_starts_with_summary_fields_and_has_feishu_sheet(self) -> None:
        from app.pipelines.wecom_structure_backup import BACKUP_SHEET_TITLES, backup_field_titles

        self.assertEqual(
            ("企微A-最新结构", "企微B-最新结构", "飞书-最新结构", "结构变更历史"),
            BACKUP_SHEET_TITLES,
        )
        self.assertEqual(
            [
                "平台",
                "来源类型",
                "智能表格名称",
                "工作表数量",
                "字段总数",
                "来源链接",
                "企业配置",
                "状态",
                "文档定位ID",
                "docid",
            ],
            backup_field_titles("企微A-最新结构", max_sheets=20)[:10],
        )

    def test_client_exposes_structure_backup_write_operations(self) -> None:
        from app.providers.wecom import WeComSmartsheetClient

        class FakeClient(WeComSmartsheetClient):
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, Any]]] = []

            def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
                self.calls.append((path, payload))
                if path == "/wedoc/create_doc":
                    return {"docid": "dc-backup"}
                return {"errcode": 0}

        client = FakeClient()
        self.assertEqual("dc-backup", client.create_doc("结构备份", ["admin-user"]))
        client.add_sheet("dc-backup", "企微A-最新结构", 1)
        client.add_fields(
            "dc-backup",
            "sheet-a",
            [{"field_title": "唯一键", "field_type": "FIELD_TYPE_TEXT"}],
        )
        client.add_records("dc-backup", "sheet-a", [{"values": {"唯一键": "A:dc-1"}}])
        client.update_records(
            "dc-backup",
            "sheet-a",
            [{"record_id": "record-1", "values": {"结构哈希": "hash-2"}}],
        )
        client.update_sheet("dc-backup", "sheet-a", "企微A-旧结构")
        client.update_fields(
            "dc-backup",
            "sheet-a",
            [{"field_id": "field-1", "field_title": "平台", "field_type": "FIELD_TYPE_TEXT"}],
        )
        client.delete_sheet("dc-backup", "default-sheet")

        paths = [path for path, _ in client.calls]
        self.assertEqual(
            [
                "/wedoc/create_doc",
                "/wedoc/smartsheet/add_sheet",
                "/wedoc/smartsheet/add_fields",
                "/wedoc/smartsheet/add_records",
                "/wedoc/smartsheet/update_records",
                "/wedoc/smartsheet/update_sheet",
                "/wedoc/smartsheet/update_fields",
                "/wedoc/smartsheet/delete_sheet",
            ],
            paths,
        )
        self.assertEqual("CELL_VALUE_KEY_TYPE_FIELD_TITLE", client.calls[3][1]["key_type"])
        self.assertEqual("CELL_VALUE_KEY_TYPE_FIELD_TITLE", client.calls[4][1]["key_type"])
        self.assertEqual(
            [{"type": "text", "text": "A:dc-1"}],
            client.calls[3][1]["records"][0]["values"]["唯一键"],
        )

    def test_rebuild_sheet_copies_records_before_deleting_old_sheet(self) -> None:
        from app.pipelines.wecom_structure_backup import rebuild_sheet_with_order

        class FakeClient:
            def __init__(self) -> None:
                self.sheets = {
                    "old": "企微A-最新结构",
                }
                self.fields = {
                    "old": [
                        {"field_id": "f-key", "field_title": "唯一键", "field_type": "FIELD_TYPE_TEXT"},
                        {"field_id": "f-name", "field_title": "智能表格名称", "field_type": "FIELD_TYPE_TEXT"},
                    ]
                }
                self.records = {
                    "old": [
                        {
                            "record_id": "r1",
                            "values": {
                                "f-key": [{"type": "text", "text": "COMPANY_A:dc-1"}],
                                "f-name": [{"type": "text", "text": "产品表"}],
                            },
                        }
                    ]
                }
                self.deleted: list[str] = []

            def get_records(self, docid: str, sheet_id: str) -> dict[str, Any]:
                return {"records": self.records.get(sheet_id, [])}

            def get_fields(self, docid: str, sheet_id: str) -> dict[str, Any]:
                return {"fields": self.fields[sheet_id]}

            def add_sheet(self, docid: str, title: str, index: int) -> None:
                self.sheets["new"] = title
                self.fields["new"] = [
                    {"field_id": "f-default", "field_title": "文本", "field_type": "FIELD_TYPE_TEXT"}
                ]
                self.records["new"] = []

            def get_sheets(self, docid: str) -> list[dict[str, Any]]:
                return [
                    {"sheet_id": sheet_id, "properties": {"title": title}}
                    for sheet_id, title in self.sheets.items()
                ]

            def update_fields(self, docid: str, sheet_id: str, fields: list[dict[str, Any]]) -> None:
                self.fields[sheet_id][0].update(fields[0])

            def add_fields(self, docid: str, sheet_id: str, fields: list[dict[str, Any]]) -> None:
                # 真实企微接口会把同一 add_fields 批次按输入逆序插入。
                self.fields[sheet_id].extend(reversed(fields))

            def add_records(self, docid: str, sheet_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
                self.records[sheet_id].extend(records)
                return {}

            def update_sheet(self, docid: str, sheet_id: str, title: str) -> None:
                self.sheets[sheet_id] = title

            def delete_sheet(self, docid: str, sheet_id: str) -> None:
                self.deleted.append(sheet_id)
                self.sheets.pop(sheet_id)

        client = FakeClient()
        new_sheet_id = rebuild_sheet_with_order(
            client,
            docid="dc-backup",
            sheet_id="old",
            sheet_title="企微A-最新结构",
            target_titles=["平台", "来源类型", "智能表格名称", "唯一键"],
            index=1,
        )

        self.assertEqual("new", new_sheet_id)
        self.assertEqual(["平台", "来源类型", "智能表格名称", "唯一键"], [f["field_title"] for f in client.fields["new"]])
        self.assertEqual(
            {
                "唯一键": [{"type": "text", "text": "COMPANY_A:dc-1"}],
                "智能表格名称": [{"type": "text", "text": "产品表"}],
            },
            client.records["new"][0]["values"],
        )
        self.assertEqual(["old"], client.deleted)
        self.assertEqual("企微A-最新结构", client.sheets["new"])

    def test_ensure_backup_workbook_creates_three_named_sheets_and_fields(self) -> None:
        from app.pipelines.wecom_structure_backup import (
            BACKUP_SHEET_TITLES,
            backup_field_titles,
            ensure_backup_workbook,
        )

        class FakeClient:
            def __init__(self) -> None:
                self.created_doc = ""
                self.sheets = [{"sheet_id": "default", "properties": {"title": "Sheet1"}}]
                self.fields: dict[str, list[dict[str, Any]]] = {"default": []}
                self.deleted: list[str] = []

            def create_doc(self, name: str, admins: list[str]) -> str:
                self.created_doc = name
                self.admins = admins
                return "dc-backup"

            def get_sheets(self, docid: str) -> list[dict[str, Any]]:
                return self.sheets

            def add_sheet(self, docid: str, title: str, index: int) -> None:
                sheet_id = f"sheet-{index}"
                self.sheets.append({"sheet_id": sheet_id, "properties": {"title": title}})
                self.fields[sheet_id] = []

            def get_fields(self, docid: str, sheet_id: str) -> dict[str, Any]:
                return {"fields": self.fields[sheet_id]}

            def add_fields(self, docid: str, sheet_id: str, fields: list[dict[str, Any]]) -> None:
                self.fields[sheet_id].extend(fields)

            def delete_sheet(self, docid: str, sheet_id: str) -> None:
                self.deleted.append(sheet_id)

        client = FakeClient()
        result = ensure_backup_workbook(client, docid="", admin_users=["admin-user"], max_sheets=20)

        self.assertEqual("dc-backup", result["docid"])
        self.assertEqual("企微智能表格结构备份", client.created_doc)
        self.assertEqual(set(BACKUP_SHEET_TITLES), set(result["sheets"]))
        self.assertEqual(["default"], client.deleted)
        for title, sheet_id in result["sheets"].items():
            actual = {field["field_title"] for field in client.fields[sheet_id]}
            self.assertEqual(set(backup_field_titles(title, max_sheets=20)), actual)


class StructureBackupWriterTests(StructureSnapshotTests):
    class FakeClient:
        def __init__(self, latest_hash: str = "old-hash", history_keys: list[str] | None = None) -> None:
            self.fields = {
                "latest-a": [
                    {"field_id": "f-key", "field_title": "唯一键"},
                    {"field_id": "f-hash", "field_title": "结构哈希"},
                    {"field_id": "f-change", "field_title": "结构最后变化时间"},
                ],
                "history": [{"field_id": "f-version", "field_title": "版本唯一键"}],
            }
            self.records = {
                "latest-a": [
                    {
                        "record_id": "record-current",
                        "values": {
                            "f-key": [{"text": "COMPANY_A:dc-1"}],
                            "f-hash": [{"text": latest_hash}],
                            "f-change": [{"text": "2026-06-20T00:00:00+00:00"}],
                        },
                    }
                ],
                "history": [
                    {"record_id": f"history-{index}", "values": {"f-version": [{"text": key}]}}
                    for index, key in enumerate(history_keys or [], start=1)
                ],
            }
            self.added: list[tuple[str, list[dict[str, Any]]]] = []
            self.updated: list[tuple[str, list[dict[str, Any]]]] = []

        def get_fields(self, docid: str, sheet_id: str) -> dict[str, Any]:
            return {"fields": self.fields[sheet_id]}

        def get_records(self, docid: str, sheet_id: str) -> dict[str, Any]:
            return {"records": self.records[sheet_id]}

        def add_records(self, docid: str, sheet_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
            self.added.append((sheet_id, records))
            return {}

        def update_records(self, docid: str, sheet_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
            self.updated.append((sheet_id, records))
            return {}

    def test_write_snapshot_updates_latest_and_adds_changed_history(self) -> None:
        from app.pipelines.wecom_structure_backup import build_document_snapshot, write_structure_snapshot

        snapshot = build_document_snapshot(self._source(), self._sheets(), max_sheets=20)
        client = self.FakeClient(latest_hash="old-hash")

        result = write_structure_snapshot(
            client,
            backup_docid="dc-backup",
            sheet_ids={"企微A-最新结构": "latest-a", "结构变更历史": "history"},
            snapshot=snapshot,
            trigger="copy-auto",
            now="2026-06-21T00:00:00+00:00",
        )

        self.assertTrue(result["changed"])
        self.assertEqual("latest-a", client.updated[0][0])
        self.assertEqual("record-current", client.updated[0][1][0]["record_id"])
        self.assertEqual("history", client.added[0][0])
        history_values = client.added[0][1][0]["values"]
        self.assertEqual(f"COMPANY_A:dc-1:{snapshot.structure_hash}", history_values["版本唯一键"])
        self.assertEqual("old-hash", history_values["上一结构哈希"])
        self.assertEqual("copy-auto", history_values["触发来源"])

    def test_write_snapshot_does_not_duplicate_unchanged_history(self) -> None:
        from app.pipelines.wecom_structure_backup import build_document_snapshot, write_structure_snapshot

        snapshot = build_document_snapshot(self._source(), self._sheets(), max_sheets=20)
        version_key = f"COMPANY_A:dc-1:{snapshot.structure_hash}"
        client = self.FakeClient(latest_hash=snapshot.structure_hash, history_keys=[version_key])

        result = write_structure_snapshot(
            client,
            backup_docid="dc-backup",
            sheet_ids={"企微A-最新结构": "latest-a", "结构变更历史": "history"},
            snapshot=snapshot,
            trigger="daily",
            now="2026-06-21T00:00:00+00:00",
        )

        self.assertFalse(result["changed"])
        self.assertEqual([], client.added)
        latest_values = client.updated[0][1][0]["values"]
        self.assertEqual("2026-06-20T00:00:00+00:00", latest_values["结构最后变化时间"])

    def test_write_feishu_snapshot_uses_feishu_latest_sheet(self) -> None:
        from app.pipelines.wecom_structure_backup import build_document_snapshot, write_structure_snapshot

        source = self._source()
        source.update(
            {
                "provider": "feishu",
                "external_doc_id": "bascn-app",
                "document_name": "飞书应用",
                "source_type": "bitable_app",
            }
        )
        snapshot = build_document_snapshot(source, self._sheets(), max_sheets=20)
        client = self.FakeClient(latest_hash="old-hash")
        client.fields["latest-f"] = client.fields.pop("latest-a")
        client.records["latest-f"] = []

        write_structure_snapshot(
            client,
            backup_docid="dc-backup",
            sheet_ids={"飞书-最新结构": "latest-f", "结构变更历史": "history"},
            snapshot=snapshot,
            trigger="daily",
            now="2026-06-22T00:00:00+00:00",
        )

        self.assertEqual("latest-f", client.added[0][0])


class StructureBackupTriggerTests(WorkerImportTestCase):
    def test_daily_enqueue_includes_backup_document_itself(self) -> None:
        from app.pipelines.wecom_structure_backup import enqueue_daily_structure_backup_jobs

        class FakeStore:
            def __init__(self) -> None:
                self.enqueued: list[tuple[int, str, str]] = []

            def list_wecom_document_structures(self) -> list[dict[str, Any]]:
                return [
                    {"id": 1, "env_profile": "COMPANY_A", "external_doc_id": "dc-a"},
                    {"id": 2, "env_profile": "COMPANY_B", "external_doc_id": "dc-b"},
                    {"id": 3, "env_profile": "COMPANY_A", "external_doc_id": "dc-backup"},
                ]

            def list_feishu_document_structures(self) -> list[dict[str, Any]]:
                return [{"id": 4, "provider": "feishu", "env_profile": "COMPANY_A", "external_doc_id": "bascn-app"}]

            def enqueue_structure_backup_job(self, source_id: int, trigger: str, event_key: str) -> None:
                self.enqueued.append((source_id, trigger, event_key))

        store = FakeStore()
        count = enqueue_daily_structure_backup_jobs(
            store,
            day="2026-06-21",
            backup_docid="dc-backup",
        )

        self.assertEqual(4, count)
        self.assertEqual(
            [
                (1, "daily", "daily:2026-06-21:COMPANY_A:dc-a"),
                (2, "daily", "daily:2026-06-21:COMPANY_B:dc-b"),
                (3, "daily", "daily:2026-06-21:COMPANY_A:dc-backup"),
                (4, "daily", "daily:2026-06-21:FEISHU:COMPANY_A:bascn-app"),
            ],
            store.enqueued,
        )

    def test_copy_auto_enqueue_only_after_successful_initial_sync(self) -> None:
        from app.pipelines.wecom_structure_backup import enqueue_copy_auto_structure_backup

        class FakeStore:
            def __init__(self) -> None:
                self.enqueued: list[tuple[int, str, str]] = []

            def enqueue_structure_backup_job(self, source_id: int, trigger: str, event_key: str) -> None:
                self.enqueued.append((source_id, trigger, event_key))

        store = FakeStore()
        request = {"id": 77, "source_id": 42, "requested_by": "copy-auto"}

        self.assertFalse(enqueue_copy_auto_structure_backup(store, request, request_status="failed"))
        self.assertTrue(enqueue_copy_auto_structure_backup(store, request, request_status="success"))
        self.assertEqual([(42, "copy-auto", "copy-auto:77:42")], store.enqueued)

    def test_refresh_backup_workbook_registers_only_structure(self) -> None:
        from app.pipelines.wecom_structure_backup import refresh_backup_workbook_structure

        class FakeStore:
            def __init__(self) -> None:
                self.document: dict[str, Any] = {}
                self.sources: list[dict[str, Any]] = []
                self.fields: list[tuple[int, list[dict[str, Any]]]] = []
                self.active_sheet_ids: list[str] = []

            def upsert_structure_document(self, **kwargs: Any) -> int:
                self.document = kwargs
                return 900

            def ensure_source(self, **kwargs: Any) -> int:
                self.sources.append(kwargs)
                return 1000 + len(self.sources)

            def replace_fields(self, source_id: int, fields: list[dict[str, Any]]) -> dict[str, str]:
                self.fields.append((source_id, fields))
                return {}

            def deactivate_missing_structure_sheets(
                self,
                *,
                provider: str,
                env_profile: str,
                external_doc_id: str,
                active_sheet_ids: list[str],
            ) -> None:
                self.active_sheet_ids = active_sheet_ids

        class FakeClient:
            def get_fields(self, docid: str, sheet_id: str) -> dict[str, Any]:
                return {
                    "fields": [
                        {
                            "field_id": f"field-{sheet_id}",
                            "field_title": "平台",
                            "field_type": "FIELD_TYPE_TEXT",
                        }
                    ]
                }

        workbook = {
            "docid": "dc-backup",
            "url": "https://doc.weixin.qq.com/smartsheet/dc-backup",
            "sheets": {
                "企微A-最新结构": "sheet-a",
                "企微B-最新结构": "sheet-b",
                "飞书-最新结构": "sheet-f",
                "结构变更历史": "sheet-h",
            },
        }
        store = FakeStore()
        source_id = refresh_backup_workbook_structure(
            store,
            FakeClient(),
            workbook=workbook,
            profile="COMPANY_A",
        )

        self.assertEqual(900, source_id)
        self.assertEqual("structure_backup_doc", store.document["source_type"])
        self.assertEqual(4, len(store.sources))
        self.assertTrue(all(item["source_type"] == "structure_backup_sheet" for item in store.sources))
        self.assertEqual(4, len(store.fields))
        self.assertEqual(["sheet-a", "sheet-b", "sheet-f", "sheet-h"], store.active_sheet_ids)


if __name__ == "__main__":
    unittest.main()
