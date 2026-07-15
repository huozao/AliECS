from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


class _Cursor:
    def __init__(self) -> None:
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, _sql, params):
        self.params = params

    def fetchone(self):
        return (41,)


class _Conn:
    def __init__(self) -> None:
        self.cursor_obj = _Cursor()
        self.committed = False

    def close(self):
        return None

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True


class BusinessAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [item for item in sys.path if item != str(BACKEND_ROOT)]

    def test_writes_actor_channel_and_safe_details(self) -> None:
        from app.business_audit import write_business_audit

        conn = _Conn()
        with patch("app.business_audit._conn", return_value=conn):
            result = write_business_audit(
                user={"uid": 7, "username": "alice", "auth_source": "miniapp"},
                action="formula.query", resource_type="formula", query={"query": "CP001"},
                result_count=2,
            )
        self.assertEqual(41, result)
        self.assertTrue(conn.committed)
        self.assertEqual(7, conn.cursor_obj.params[0])
        self.assertEqual("alice", conn.cursor_obj.params[1])
        self.assertEqual("miniapp", conn.cursor_obj.params[3])

    def test_required_audit_failure_rejects_production_operation(self) -> None:
        from app.business_audit import write_business_audit

        with patch("app.business_audit._conn", side_effect=RuntimeError("db down")), patch.dict(
            "os.environ", {"ENV": "prod", "DATABASE_URL": "postgresql://test"}
        ):
            with self.assertRaises(HTTPException) as ctx:
                write_business_audit(user={"uid": 7}, action="formula.download", required=True)
        self.assertEqual(503, ctx.exception.status_code)

    def test_migration_contains_trace_fields_and_no_secret_fields(self) -> None:
        sql = (ROOT / "db" / "migrations" / "0032_business_audit_events.sql").read_text(encoding="utf-8")
        for field in ("actor_user_id", "auth_source", "client_channel", "request_id", "file_sha256"):
            self.assertIn(field, sql)
        self.assertNotIn("bearer_token", sql.lower())
        self.assertNotIn("password TEXT", sql)


if __name__ == "__main__":
    unittest.main()
