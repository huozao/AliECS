from __future__ import annotations

import unittest
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))

from app.integrations.chanjet.schemas import ChanjetEvent
from app.integrations.events import (
    build_chanjet_bom_sync_request,
    build_ops_attention_items,
    stable_json_hash,
)


class IntegrationEventTests(unittest.TestCase):
    def test_chanjet_bom_update_builds_incremental_sync_request(self) -> None:
        event = ChanjetEvent(
            event_id="evt-1",
            msg_type="Bom_Update",
            app_key="demo",
            app_id="45057",
            received_time="1760000000000",
            biz_content={"Code": "HYD-4197PC", "Version": "2026-06-03F"},
            raw={"msgType": "Bom_Update"},
        )

        request = build_chanjet_bom_sync_request(event)

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("chanjet", request["provider"])
        self.assertEqual("bom", request["module"])
        self.assertEqual("incremental", request["mode"])
        self.assertEqual("Bom_Update", request["target_json"]["event_type"])
        self.assertEqual("HYD-4197PC", request["target_json"]["parent_code"])
        self.assertEqual("2026-06-03F", request["target_json"]["version"])
        self.assertTrue(request["target_json"]["include_disabled"])

    def test_chanjet_bom_event_without_target_falls_back_to_full_bom(self) -> None:
        event = ChanjetEvent(
            event_id="evt-2",
            msg_type="Bom_Close",
            app_key=None,
            app_id=None,
            received_time=None,
            biz_content={"unknown": "value"},
            raw={"msgType": "Bom_Close"},
        )

        request = build_chanjet_bom_sync_request(event)

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("full_bom", request["mode"])
        self.assertEqual("missing_bom_target", request["target_json"]["fallback_reason"])

    def test_non_bom_chanjet_event_does_not_build_sync_request(self) -> None:
        event = ChanjetEvent(
            event_id="evt-3",
            msg_type="APP_TICKET",
            app_key=None,
            app_id=None,
            received_time=None,
            biz_content={},
            raw={"msgType": "APP_TICKET"},
        )

        self.assertIsNone(build_chanjet_bom_sync_request(event))

    def test_stable_json_hash_is_order_independent(self) -> None:
        self.assertEqual(
            stable_json_hash({"a": 1, "b": [2, 3]}),
            stable_json_hash({"b": [2, 3], "a": 1}),
        )

    def test_ops_attention_items_surface_failures_and_diffs(self) -> None:
        status = {
            "database": {"ok": True},
            "tplus": {"pending_requests": 3, "failed_requests": 1, "last_success_at": None},
            "reconciliation": {"needs_review": 2},
            "system": {"disk_percent": 91.2, "memory_percent": 82.0},
            "hosts": [{"name": "old-laptop", "ok": False, "message": "timeout"}],
        }

        items = build_ops_attention_items(status)

        codes = [item["code"] for item in items]
        self.assertIn("tplus_failed_requests", codes)
        self.assertIn("reconciliation_needs_review", codes)
        self.assertIn("disk_high", codes)
        self.assertIn("host_unreachable", codes)


if __name__ == "__main__":
    unittest.main()
