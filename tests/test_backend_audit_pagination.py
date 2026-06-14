from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_main():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import main

    return main


class AuditPaginationTests(unittest.TestCase):
    def test_audit_logs_paginated(self) -> None:
        main = load_main()
        rows = [
            (i, "admin", f"action.{i}", "target", str(i), {"n": i}, datetime(2026, 6, 14, tzinfo=timezone.utc))
            for i in range(250, 0, -1)
        ]
        conn = FakeAuditConn(rows)

        with patch.object(main, "_conn", return_value=conn):
            first = main.admin_audit_logs(page=1, page_size=50, _={})
            second = main.admin_audit_logs(page=2, page_size=50, _={})

        self.assertEqual(50, len(first["items"]))
        self.assertEqual(250, first["total"])
        self.assertEqual(1, first["page"])
        self.assertEqual(50, first["page_size"])
        ids1 = {item["id"] for item in first["items"]}
        ids2 = {item["id"] for item in second["items"]}
        self.assertTrue(ids1.isdisjoint(ids2))

    def test_audit_logs_clamps_page_size(self) -> None:
        main = load_main()
        conn = FakeAuditConn([])

        with patch.object(main, "_conn", return_value=conn):
            body = main.admin_audit_logs(page=0, page_size=500, _={})

        self.assertEqual(1, body["page"])
        self.assertEqual(200, body["page_size"])


class FakeAuditConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeAuditCursor(self.rows)

    def close(self) -> None:
        pass


class FakeAuditCursor:
    def __init__(self, rows):
        self.rows = rows
        self._one = None
        self._many = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("select count(*) from audit_logs"):
            self._one = (len(self.rows),)
            self._many = []
            return
        if "from audit_logs" in normalized and "limit %s offset %s" in normalized:
            limit, offset = params
            self._many = self.rows[offset : offset + limit]
            self._one = None
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


if __name__ == "__main__":
    unittest.main()
