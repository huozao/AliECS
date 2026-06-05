from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))

from app.integrations.chanjet.schemas import ChanjetEvent
from app.integrations.store import save_chanjet_event_and_queue_request


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.ids = iter([(101,), (202,)])

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.statements.append((sql, params))

    def fetchone(self) -> tuple[int]:
        return next(self.ids)


class FakeConn:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()
        self.committed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True


class IntegrationStoreTests(unittest.TestCase):
    def test_save_chanjet_bom_event_creates_sync_request(self) -> None:
        conn = FakeConn()
        event = ChanjetEvent(
            event_id="evt-store-1",
            msg_type="Bom_Update",
            app_key="demo",
            app_id="45057",
            received_time="1760000000000",
            biz_content={"Code": "HYD-4197PC", "Version": "2026-06-03F"},
            raw={"msgType": "Bom_Update"},
        )
        record = {"event_id": event.event_id, "msg_type": event.msg_type, "biz_content": event.biz_content}

        result = save_chanjet_event_and_queue_request(conn, event, record)

        self.assertTrue(conn.committed)
        self.assertEqual(101, result["event_id"])
        self.assertEqual(202, result["sync_request_id"])
        joined_sql = "\n".join(statement for statement, _ in conn.cursor_obj.statements)
        self.assertIn("integration_events", joined_sql)
        self.assertIn("integration_sync_requests", joined_sql)


if __name__ == "__main__":
    unittest.main()
