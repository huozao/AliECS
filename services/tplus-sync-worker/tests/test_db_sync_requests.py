import unittest
from typing import Any

from tplus_datahub.jobs import db_sync_requests


class FakeCursor:
    def __init__(self):
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.rows = [
            (
                7,
                "incremental",
                {"parent_code": "HYD-4197PC"},
                "evt-1",
            )
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=()):
        self.statements.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def transaction(self):
        return FakeTransaction()


class DbSyncRequestTests(unittest.TestCase):
    def test_fetch_next_bom_request_marks_request_running(self):
        conn = FakeConn()

        request = db_sync_requests.fetch_next_bom_request(conn)

        self.assertEqual(7, request["id"])
        self.assertEqual("incremental", request["mode"])
        self.assertEqual({"parent_code": "HYD-4197PC"}, request["target_json"])
        joined_sql = "\n".join(statement for statement, _ in conn.cursor_obj.statements)
        self.assertIn("FOR UPDATE SKIP LOCKED", joined_sql)
        self.assertIn("UPDATE integration_sync_requests", joined_sql)


if __name__ == "__main__":
    unittest.main()
