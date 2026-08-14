from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "services" / "backend-api"
sys.path.insert(0, str(BACKEND))

from app import document_locator
from app.integrations import wecom_docs


VALID_DOCID = "d" + "c" + ("q" * 86)


class QueueCursor:
    def __init__(self, responses: list[list[tuple[Any, ...]]]) -> None:
        self.responses = responses
        self.current: list[tuple[Any, ...]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed.append((sql, tuple(params or ())))
        self.current = self.responses.pop(0) if self.responses else []

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return list(self.current)


class QueueConnection:
    def __init__(self, responses: list[list[tuple[Any, ...]]]) -> None:
        self.cursor_value = QueueCursor(responses)
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self):
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class ConnectionQueue:
    def __init__(self, connections: list[QueueConnection]) -> None:
        self.connections = list(connections)

    def __call__(self) -> QueueConnection:
        return self.connections.pop(0)


class DocumentLocatorBackendTests(unittest.TestCase):
    def test_catalog_is_canonical_includes_system_and_unresolved_without_external_ids(self) -> None:
        conn = QueueConnection([[
            ("wecom", "COMPANY_A", VALID_DOCID, "smartsheet_doc", "生产表", "生产表", 11, 2, 2, None, "verified", {"read": "verified", "copy": "allowed"}, "active"),
            ("wecom", "COMPANY_A", VALID_DOCID, "structure_backup_doc", "企微智能表格结构备份", "", 12, 0, 0, None, "verified", {"read": "verified", "copy": "unavailable"}, "active"),
            ("wecom", "COMPANY_A", "s3_" + ("x" * 40), "smartsheet_link", "产品名称命名", "", 13, 0, 0, None, "invalid-id", {"read": "unavailable", "copy": "unavailable"}, "unresolved"),
            ("wecom", "COMPANY_A", "s3_" + ("y" * 40), "registry", "孤立登记", "", None, 0, 0, None, "invalid-id", {"read": "unavailable", "copy": "unavailable"}, "unresolved"),
            ("wecom", "COMPANY_A", VALID_DOCID, "registry_doc", "权限失效", "", 14, 2, 2, None, "permission-denied", {"read": "unavailable", "copy": "unavailable"}, "active"),
        ]])

        result = document_locator.asset_catalog(
            conn,
            tplus_items=[{"name": "bom", "download_url": "/v1/exports/tplus/bom.xlsx"}],
        )

        items = result["groups"][1]["items"]
        # 未关联内部来源的档案只展示，不带 source_id 键，避免前端拼出无效内部地址。
        self.assertEqual([11, 12, 13, None, 14], [item.get("source_id") for item in items])
        self.assertNotIn("source_id", items[3])
        self.assertTrue(items[0]["can_sync"] and items[0]["can_download"] and items[0]["can_copy"])
        self.assertTrue(items[1]["system_managed"])
        self.assertTrue(items[1]["can_download"])
        self.assertFalse(items[1]["can_sync"] or items[1]["can_copy"])
        self.assertEqual("缺少有效企微 docid", items[2]["reason"])
        self.assertFalse(items[2]["can_sync"] or items[2]["can_download"] or items[2]["can_copy"])
        self.assertEqual("权限验证失败", items[4]["reason"])
        self.assertFalse(items[4]["can_sync"] or items[4]["can_download"] or items[4]["can_copy"])
        self.assertTrue(all(str(item.get("download_url") or "").startswith("/v1/sync/") for item in items if item["can_download"]))
        self.assertIn("from document_locator_registry", conn.cursor_value.executed[0][0].lower())
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        self.assertNotIn(VALID_DOCID, serialized)
        self.assertNotIn("s3_", serialized)

    def test_copy_resumes_external_created_without_calling_provider_and_returns_internal_ids_only(self) -> None:
        lookup = QueueConnection([
            [(5, "external_created", VALID_DOCID, None, None, 11, "生产表-副本")],
            [("wecom", "COMPANY_A", VALID_DOCID, "smartsheet_doc", "生产表", "active")],
        ])
        register = QueueConnection([
            [(42,)],
            [(9, 1)],
            [],
            [],
            [(77,)],
            [],
        ])
        copier = mock.Mock(side_effect=AssertionError("provider must not run twice"))

        result = document_locator.copy_asset(
            ConnectionQueue([lookup, register]),
            source_id=11,
            idempotency_key="copy-action-1",
            requested_by="admin",
            copier=copier,
        )

        self.assertEqual(
            {"status": "registered", "copy_request_id": 5, "source_id": 42, "locator_id": 9, "sync_request_id": 77},
            result,
        )
        copier.assert_not_called()
        self.assertNotIn(VALID_DOCID, json.dumps(result))
        self.assertEqual(1, register.commits)

    def test_copy_registered_retry_returns_the_created_source_id(self) -> None:
        lookup = QueueConnection([[
            (5, "registered", VALID_DOCID, 9, 77, 11, "生产表-副本", 42),
        ]])
        copier = mock.Mock(side_effect=AssertionError("registered retry must not call provider"))

        result = document_locator.copy_asset(
            ConnectionQueue([lookup]),
            source_id=11,
            idempotency_key="copy-action-registered",
            requested_by="admin",
            copier=copier,
        )

        self.assertEqual(
            {"status": "registered", "copy_request_id": 5, "source_id": 42, "locator_id": 9, "sync_request_id": 77},
            result,
        )
        copier.assert_not_called()

    def test_new_copy_persists_external_identity_before_registration(self) -> None:
        prepare = QueueConnection([
            [],
            [("wecom", "COMPANY_A", VALID_DOCID, "smartsheet_doc", "生产表", "active")],
            [("verified", {"read": "verified", "copy": "allowed"})],
            [(6,)],
        ])
        external = QueueConnection([[]])
        completed = QueueConnection([[]])
        register = QueueConnection([[(43,)], [(10, 1)], [], [], [(78,)], []])
        copier = mock.Mock(return_value={"new_docid": VALID_DOCID, "url": "https://doc.weixin.qq.com/smartsheet/synthetic"})

        result = document_locator.copy_asset(
            ConnectionQueue([prepare, external, completed, register]),
            source_id=11,
            idempotency_key="copy-action-2",
            requested_by="admin",
            copier=copier,
            now_name=lambda name: name + "-副本",
        )

        self.assertEqual("registered", result["status"])
        copier.assert_called_once()
        self.assertEqual(1, external.commits)
        external_update = external.cursor_value.executed[0]
        self.assertIn("copying", external_update[0])
        self.assertIn(VALID_DOCID, external_update[1])
        self.assertIn("external_created", completed.cursor_value.executed[0][0])
        insert_sql = prepare.cursor_value.executed[-1][0].lower()
        self.assertIn("'creating'", insert_sql)
        self.assertIn("on conflict", insert_sql)

    def test_copy_in_progress_same_key_never_calls_provider_again(self) -> None:
        # creating：企微还没返回 docid，结果不确定，只能人工核对。
        lookup = QueueConnection([[
            (7, "creating", None, None, None, 11, "生产表-副本", None),
        ]])
        copier = mock.Mock()

        with self.assertRaisesRegex(document_locator.InvalidLocatorAction, "处理中"):
            document_locator.copy_asset(
                ConnectionQueue([lookup]),
                source_id=11,
                idempotency_key="copy-action-in-progress",
                requested_by="admin",
                copier=copier,
            )

        copier.assert_not_called()

    def test_copy_resumes_copying_request_and_records_incomplete_content(self) -> None:
        # copying：企微文档已建、工作表没复制完；补登记而不是再建一个，历史里留 resumed。
        lookup = QueueConnection([
            [(7, "copying", VALID_DOCID, None, None, 11, "生产表-副本")],
            [("wecom", "COMPANY_A", VALID_DOCID, "smartsheet_doc", "生产表", "active")],
        ])
        register = QueueConnection([[(44,)], [(12, 3)], [], [], [(79,)], []])
        copier = mock.Mock(side_effect=AssertionError("resume must not create another copy"))

        result = document_locator.copy_asset(
            ConnectionQueue([lookup, register]),
            source_id=11,
            idempotency_key="copy-action-resume",
            requested_by="admin",
            copier=copier,
        )

        self.assertEqual("registered", result["status"])
        self.assertEqual(44, result["source_id"])
        copier.assert_not_called()
        event_params = register.cursor_value.executed[2][1]
        summary = next(
            param for param in event_params
            if hasattr(param, "obj") and isinstance(param.obj, dict) and "status" in param.obj
        )
        self.assertTrue(summary.obj.get("resumed"))
        self.assertNotIn(VALID_DOCID, json.dumps(result))

    def test_copy_refuses_to_resume_without_a_valid_docid(self) -> None:
        lookup = QueueConnection([[
            (7, "copying", "s3_" + ("z" * 40), None, None, 11, "生产表-副本"),
        ]])
        copier = mock.Mock()

        with self.assertRaisesRegex(document_locator.InvalidLocatorAction, "有效 docid"):
            document_locator.copy_asset(
                ConnectionQueue([lookup]),
                source_id=11,
                idempotency_key="copy-action-resume-invalid",
                requested_by="admin",
                copier=copier,
            )

        copier.assert_not_called()

    def test_copy_persists_created_docid_before_later_provider_failure(self) -> None:
        prepare = QueueConnection([
            [],
            [("wecom", "COMPANY_A", VALID_DOCID, "smartsheet_doc", "生产表", "active")],
            [("verified", {"read": "verified", "copy": "allowed"})],
            [(8,)],
        ])
        created = QueueConnection([[]])
        failed = QueueConnection([[]])

        def copier(**kwargs: Any) -> dict[str, Any]:
            kwargs["on_created"](VALID_DOCID, "https://example.invalid/synthetic")
            raise RuntimeError("synthetic failure after remote create")

        with self.assertRaisesRegex(RuntimeError, "after remote create"):
            document_locator.copy_asset(
                ConnectionQueue([prepare, created, failed]),
                source_id=11,
                idempotency_key="copy-action-created-first",
                requested_by="admin",
                copier=copier,
            )

        self.assertIn("copying", created.cursor_value.executed[0][0])
        self.assertEqual(VALID_DOCID, created.cursor_value.executed[0][1][0])
        self.assertIn("new_api_doc_id IS NULL", failed.cursor_value.executed[0][0])

    def test_copy_rejects_unknown_copy_capability(self) -> None:
        lookup = QueueConnection([
            [],
            [("wecom", "COMPANY_A", VALID_DOCID, "smartsheet_doc", "生产表", "active")],
            [("verified", {"read": "verified", "copy": "unverified"})],
        ])

        with self.assertRaisesRegex(document_locator.InvalidLocatorAction, "权限"):
            document_locator.copy_asset(
                ConnectionQueue([lookup]),
                source_id=11,
                idempotency_key="copy-action-unknown",
                requested_by="admin",
                copier=mock.Mock(),
            )

    def test_provider_reports_new_docid_immediately_after_create(self) -> None:
        client = mock.Mock()
        client.get_sheets.return_value = [{"sheet_id": "source-sheet"}]
        client.create_doc.return_value = VALID_DOCID
        created = mock.Mock(side_effect=RuntimeError("stop after identity persistence"))

        with mock.patch.object(wecom_docs, "credentials_for_profile", return_value=("corp", "secret")), mock.patch.object(
            wecom_docs, "doc_admin_users", return_value=["admin"]
        ), mock.patch.object(wecom_docs, "WeComDocClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "identity persistence"):
                wecom_docs.copy_smartsheet_doc("COMPANY_A", VALID_DOCID, "副本", on_created=created)

        created.assert_called_once_with(VALID_DOCID, f"https://doc.weixin.qq.com/smartsheet/{VALID_DOCID}")
        client.create_doc.assert_called_once_with("副本", ["admin"])
        client.get_sheets.assert_called_once_with(VALID_DOCID)

    def test_copy_rejects_locator_without_verified_read_permission(self) -> None:
        lookup = QueueConnection([
            [],
            [("wecom", "COMPANY_A", VALID_DOCID, "smartsheet_doc", "生产表", "active")],
            [("permission-denied", {"read": "unavailable", "copy": "unavailable"})],
        ])
        copier = mock.Mock()

        with self.assertRaisesRegex(document_locator.InvalidLocatorAction, "权限"):
            document_locator.copy_asset(
                ConnectionQueue([lookup]),
                source_id=11,
                idempotency_key="copy-action-denied",
                requested_by="admin",
                copier=copier,
            )

        copier.assert_not_called()

    def test_repair_rejects_invalid_id_before_provider_or_database_write(self) -> None:
        client_factory = mock.Mock()
        with self.assertRaises(document_locator.InvalidLocatorAction):
            document_locator.repair_docid(
                ConnectionQueue([]),
                source_id=13,
                api_doc_id="s3_" + ("x" * 40),
                requested_by="admin",
                client_factory=client_factory,
            )
        client_factory.assert_not_called()

    def test_repair_verifies_exact_docid_and_returns_no_identifier(self) -> None:
        read = QueueConnection([
            [("wecom", "COMPANY_A", "s3_" + ("x" * 40), "smartsheet_link", "旧名", "active")],
        ])
        write = QueueConnection([
            [],
            [],
            [],
            [(19, 1)],
            [],
            [],
            [(81,)],
        ])
        client = mock.Mock()
        client.get_doc_name.return_value = "实时名称"
        client.get_sheets.return_value = [{"sheet_id": "sheet-1"}]

        result = document_locator.repair_docid(
            ConnectionQueue([read, write]),
            source_id=13,
            api_doc_id=VALID_DOCID,
            requested_by="admin",
            client_factory=lambda profile: client,
        )

        self.assertEqual({"status": "registered", "source_id": 13, "locator_id": 19, "sync_request_id": 81}, result)
        client.get_doc_name.assert_called_once_with(VALID_DOCID)
        client.get_sheets.assert_called_once_with(VALID_DOCID)
        self.assertNotIn(VALID_DOCID, json.dumps(result))
        self.assertEqual(1, write.commits)

    def test_repair_merges_into_existing_resolved_locator_without_duplicate_api_identity(self) -> None:
        read = QueueConnection([[
            ("wecom", "COMPANY_A", "s3_" + ("x" * 40), "smartsheet_link", "旧名", "active"),
        ]])
        write = QueueConnection([
            [(22,)],
            [],
            [(30, 4)],
            [(30, 5)],
            [(19, 2)],
            [],
            [],
            [],
            [],
            [(82,)],
        ])
        client = mock.Mock()
        client.get_doc_name.return_value = "实时名称"
        client.get_sheets.return_value = [{"sheet_id": "sheet-1"}]

        result = document_locator.repair_docid(
            ConnectionQueue([read, write]),
            source_id=13,
            api_doc_id=VALID_DOCID,
            requested_by="admin",
            client_factory=lambda profile: client,
        )

        self.assertEqual({"status": "registered", "source_id": 22, "locator_id": 30, "sync_request_id": 82}, result)
        statements = [sql.lower() for sql, _ in write.cursor_value.executed]
        self.assertTrue(any("where provider = 'wecom'" in sql and "from document_locator_registry" in sql for sql in statements))
        unresolved_update = next(sql for sql in statements if "lifecycle_status = 'disabled'" in sql)
        self.assertNotIn("api_doc_id =", unresolved_update)


if __name__ == "__main__":
    unittest.main()
