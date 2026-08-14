from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))

from app import sync_control


class FakeCursor:
    def __init__(self, responses: list[list[tuple[Any, ...]]] | None = None) -> None:
        self.responses = list(responses or [])
        self.current: list[tuple[Any, ...]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed.append((sql, tuple(params or ())))
        self.current = self.responses.pop(0) if self.responses else []

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return list(self.current)


class FakeConn:
    def __init__(self, responses: list[list[tuple[Any, ...]]] | None = None) -> None:
        self.cur = FakeCursor(responses)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


class SyncControlTests(unittest.TestCase):
    def test_source_group_uses_fixed_four_categories(self) -> None:
        self.assertEqual("tplus", sync_control.source_group("chanjet", ""))
        self.assertEqual("feishu", sync_control.source_group("feishu", "COMPANY_A"))
        self.assertEqual("wecom_company_a", sync_control.source_group("wecom", "COMPANY_A"))
        self.assertEqual("wecom_company_b", sync_control.source_group("wecom", "COMPANY_B"))

    def test_wecom_docid_validation_rejects_share_ids_and_short_values(self) -> None:
        self.assertTrue(sync_control.valid_wecom_docid("dc" + "d" * 86))
        for invalid in ("", "s3_" + "x" * 40, "dc-short", "DC" + "d" * 86):
            with self.subTest(invalid=invalid[:8]):
                self.assertFalse(sync_control.valid_wecom_docid(invalid))

    def test_assets_use_four_groups_without_external_ids(self) -> None:
        valid_docid = "dc" + "d" * 86
        conn = FakeConn(
            [[
                ("wecom", "COMPANY_A", valid_docid, "smartsheet_doc", "生产表", "生产表", 11, 2, 2, None, "verified", {"read": "verified", "copy": "allowed"}, "active"),
                ("wecom", "COMPANY_B", "s3_" + "x" * 40, "smartsheet_link", "历史链接", "历史链接", 12, 0, 0, None, "invalid-id", {"read": "unavailable", "copy": "unavailable"}, "unresolved"),
                ("feishu", "COMPANY_A", "app-test-token", "bitable_app", "飞书表", "飞书表", 13, 3, 2, None, "verified", {"read": "verified", "copy": "unavailable"}, "active"),
            ]]
        )

        result = sync_control.assets(
            conn,
            tplus_items=[{"name": "bom", "download_url": "/v1/exports/tplus/bom.xlsx"}],
        )

        self.assertEqual(
            ["tplus", "wecom_company_a", "wecom_company_b", "feishu"],
            [group["key"] for group in result["groups"]],
        )
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        self.assertNotIn("external_doc_id", serialized)
        self.assertNotIn(valid_docid, serialized)
        self.assertNotIn("s3_", serialized)
        company_b = result["groups"][2]["items"][0]
        self.assertFalse(company_b["syncable"])
        self.assertEqual("缺少有效企微 docid", company_b["reason"])
        self.assertEqual(12, company_b["source_id"])
        self.assertFalse(company_b["can_download"] or company_b["can_copy"])
        feishu = result["groups"][3]["items"][0]
        self.assertTrue(feishu["syncable"])
        self.assertEqual(13, feishu["source_id"])

    def test_enqueue_doc_asset_rejects_link_without_insert(self) -> None:
        conn = FakeConn(
            [[("wecom", "COMPANY_A", "s3_" + "x" * 40, "smartsheet_link", "历史链接", "active")]]
        )

        with self.assertRaises(sync_control.InvalidSyncTarget):
            sync_control.enqueue_doc_asset(conn, 12, "admin")

        self.assertEqual(1, len(conn.cur.executed))
        self.assertFalse(any("INSERT INTO sync_requests" in sql for sql, _ in conn.cur.executed))
        self.assertEqual(1, conn.rollbacks)

    def test_enqueue_doc_asset_dedupes_pending_request(self) -> None:
        valid_docid = "dc" + "d" * 86
        conn = FakeConn(
            [
                [("wecom", "COMPANY_A", valid_docid, "smartsheet_doc", "生产表", "active")],
                [(91, "pending")],
            ]
        )

        result = sync_control.enqueue_doc_asset(conn, 11, "admin")

        self.assertEqual(False, result["queued"])
        self.assertEqual(91, result["request_id"])
        self.assertEqual("pending", result["status"])
        self.assertFalse(any("INSERT INTO sync_requests" in sql for sql, _ in conn.cur.executed))
        self.assertEqual(1, conn.commits)

    def test_enqueue_doc_asset_inserts_valid_anchor_without_external_id_parameter(self) -> None:
        valid_docid = "dc" + "d" * 86
        conn = FakeConn(
            [
                [("wecom", "COMPANY_A", valid_docid, "registry_doc", "生产表", "active")],
                [],
                [(101,)],
            ]
        )

        result = sync_control.enqueue_doc_asset(conn, 11, "admin")

        self.assertEqual({"queued": True, "request_id": 101, "status": "pending", "document_name": "生产表"}, result)
        insert = next(item for item in conn.cur.executed if "INSERT INTO sync_requests" in item[0])
        self.assertEqual((11, "wecom", "COMPANY_A", "admin"), insert[1])
        self.assertNotIn(valid_docid, insert[1])
        self.assertEqual(1, conn.commits)

    def test_enqueue_tplus_full_dedupes_running_request(self) -> None:
        conn = FakeConn([[(77, "running")]])

        result = sync_control.enqueue_tplus_full(conn, "admin")

        self.assertEqual(False, result["queued"])
        self.assertEqual(77, result["request_id"])
        self.assertFalse(any("INSERT INTO integration_sync_requests" in sql for sql, _ in conn.cur.executed))
        self.assertEqual(1, conn.commits)

    def test_enqueue_all_queues_only_eligible_docs_and_one_tplus(self) -> None:
        conn = FakeConn(
            [
                [[11, "wecom", "COMPANY_A", "生产表"], [13, "feishu", "COMPANY_A", "飞书表"]],
                [],
                [(201,)],
                [(202, "running")],
                [],
                [(301,)],
            ]
        )

        result = sync_control.enqueue_all(conn, "admin")

        self.assertEqual(1, result["documents_queued"])
        self.assertEqual(1, result["documents_skipped"])
        self.assertTrue(result["tplus_queued"])
        self.assertEqual(1, conn.commits)
        inserts = [sql for sql, _ in conn.cur.executed if "INSERT INTO sync_requests" in sql]
        self.assertEqual(1, len(inserts))

    def test_enqueue_all_rolls_back_on_database_failure(self) -> None:
        class FailingCursor(FakeCursor):
            def execute(self, sql: str, params=None) -> None:
                if "INSERT INTO sync_requests" in sql:
                    raise RuntimeError("database secret detail")
                super().execute(sql, params)

        conn = FakeConn([[[11, "wecom", "COMPANY_A", "生产表"]], []])
        conn.cur = FailingCursor(conn.cur.responses)

        with self.assertRaises(RuntimeError):
            sync_control.enqueue_all(conn, "admin")

        self.assertEqual(0, conn.commits)
        self.assertEqual(1, conn.rollbacks)


if __name__ == "__main__":
    unittest.main()
