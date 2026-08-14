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
            ("wecom", "COMPANY_A", VALID_DOCID, "smartsheet_doc", "生产表", "生产表", 11, 2, 2, None),
            ("wecom", "COMPANY_A", VALID_DOCID, "structure_backup_doc", "企微智能表格结构备份", "", 12, 2, 0, None),
            ("wecom", "COMPANY_A", "s3_" + ("x" * 40), "smartsheet_link", "产品名称命名", "", 13, 0, 0, None),
        ]])

        result = document_locator.asset_catalog(
            conn,
            tplus_items=[{"name": "bom", "download_url": "/v1/exports/tplus/bom.xlsx"}],
        )

        items = result["groups"][1]["items"]
        self.assertEqual([11, 12, 13], [item["source_id"] for item in items])
        self.assertTrue(items[0]["can_sync"] and items[0]["can_download"] and items[0]["can_copy"])
        self.assertTrue(items[1]["system_managed"])
        self.assertTrue(items[1]["can_download"])
        self.assertFalse(items[1]["can_sync"] or items[1]["can_copy"])
        self.assertEqual("缺少有效企微 docid", items[2]["reason"])
        self.assertFalse(items[2]["can_sync"] or items[2]["can_download"] or items[2]["can_copy"])
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
            [(6,)],
        ])
        external = QueueConnection([[]])
        register = QueueConnection([[(43,)], [(10, 1)], [], [], [(78,)], []])
        copier = mock.Mock(return_value={"new_docid": VALID_DOCID, "url": "https://doc.weixin.qq.com/smartsheet/synthetic"})

        result = document_locator.copy_asset(
            ConnectionQueue([prepare, external, register]),
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
        self.assertIn("external_created", external_update[0])
        self.assertIn(VALID_DOCID, external_update[1])

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


if __name__ == "__main__":
    unittest.main()
