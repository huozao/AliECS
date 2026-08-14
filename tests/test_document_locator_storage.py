from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "services" / "doc-sync-worker"
sys.path.insert(0, str(WORKER))

from app.storage.postgres import PostgresDocSyncStore  # noqa: E402


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn
        self._row: Any = None
        self._rows: list[Any] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        normalized = " ".join(sql.split()).lower()
        self.conn.calls.append((normalized, params))
        if self.conn.fail_on and self.conn.fail_on in normalized:
            raise RuntimeError("injected locator write failure")
        response = self.conn.responses.pop(0) if self.conn.responses else None
        if isinstance(response, list):
            self._rows = response
            self._row = response[0] if response else None
        else:
            self._row = response
            self._rows = [] if response is None else [response]

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class FakeConn:
    def __init__(self, responses: list[Any] | None = None, fail_on: str = "") -> None:
        self.responses = list(responses or [])
        self.fail_on = fail_on
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def locator() -> dict[str, Any]:
    return {
        "provider": "wecom",
        "env_profile": "COMPANY_A",
        "api_doc_id": "dc-fixture",
        "share_ref": "s3_fixture",
        "document_name": "fixture",
        "source_url": "https://example.invalid/share",
        "admin_userids": ["admin-fixture"],
        "credential_ref": "COMPANY_A_PRIMARY",
        "source_kind": "registry",
        "lifecycle_status": "active",
        "syncability_status": "verified",
        "capabilities": {"read": "verified", "write": "unknown", "copy": "verified"},
        "sheet_count": 3,
        "external_source_id": 17,
        "last_verified_at": None,
        "last_sync_at": None,
        "last_error_code": "",
        "last_error_summary": "",
    }


class DocumentLocatorStorageTests(unittest.TestCase):
    def test_store_exposes_locator_and_mirror_job_contract(self) -> None:
        for name in (
            "upsert_document_locator",
            "enqueue_document_locator_mirror",
            "claim_document_locator_mirror_jobs",
            "finish_document_locator_mirror_job",
            "retry_document_locator_mirror_job",
        ):
            self.assertTrue(hasattr(PostgresDocSyncStore, name), name)

    def test_new_locator_writes_event_and_mirror_in_one_commit(self) -> None:
        conn = FakeConn(responses=[None, (41, 1), None, (81,)])
        store = PostgresDocSyncStore(conn)

        result = store.upsert_document_locator(locator(), event_type="registry-import", actor="importer")

        self.assertEqual(
            {"id": 41, "locator_version": 1, "changed": True, "created": True, "mirror_job_id": 81},
            result,
        )
        statements = [sql for sql, _ in conn.calls]
        self.assertTrue(any("insert into document_locator_registry" in sql for sql in statements))
        locator_insert = next(sql for sql in statements if "insert into document_locator_registry" in sql)
        self.assertIn("updated_at", locator_insert)
        self.assertIn("now()", locator_insert)
        self.assertTrue(any("insert into document_locator_events" in sql for sql in statements))
        self.assertTrue(any("insert into document_locator_mirror_jobs" in sql for sql in statements))
        self.assertEqual(1, conn.commits)
        self.assertEqual(0, conn.rollbacks)

    def test_event_failure_rolls_back_locator_and_mirror(self) -> None:
        conn = FakeConn(responses=[None, (41, 1)], fail_on="insert into document_locator_events")
        store = PostgresDocSyncStore(conn)

        with self.assertRaisesRegex(RuntimeError, "injected locator write failure"):
            store.upsert_document_locator(locator(), event_type="registry-import", actor="importer")

        self.assertEqual(0, conn.commits)
        self.assertEqual(1, conn.rollbacks)

    def test_claim_uses_skip_locked_and_retry_redacts_error(self) -> None:
        conn = FakeConn(responses=[[(81, 41, 1, "sync-success", 0)]])
        store = PostgresDocSyncStore(conn)

        jobs = store.claim_document_locator_mirror_jobs(limit=5)

        self.assertEqual(81, jobs[0]["id"])
        self.assertIn("for update skip locked", conn.calls[0][0])
        self.assertEqual(1, conn.commits)

        conn.responses = [None]
        store.retry_document_locator_mirror_job(81, "secret=" + "x" * 3000, 60)
        retry_params = conn.calls[-1][1]
        self.assertIn("secret=[redacted]", retry_params[0])
        self.assertNotIn("x" * 20, retry_params[0])
        self.assertLessEqual(len(retry_params[0]), 500)


if __name__ == "__main__":
    unittest.main()
