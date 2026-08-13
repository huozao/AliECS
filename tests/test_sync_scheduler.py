from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC_ROOT = ROOT / "services" / "doc-sync-worker"
TPLUS_SERVICE_ROOT = ROOT / "services" / "tplus-sync-worker"
TPLUS_ROOT = ROOT / "services" / "tplus-sync-worker" / "src"
DOC_SCHEDULER = DOC_ROOT / "app" / "pipelines" / "sync_scheduler.py"
TPLUS_SCHEDULER = TPLUS_ROOT / "tplus_datahub" / "jobs" / "sync_scheduler.py"
NOW = datetime(2026, 8, 13, 11, 0, tzinfo=timezone.utc)
LAST_FULL = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SchedulerKernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scheduler = _load_module("doc_sync_scheduler_kernel", DOC_SCHEDULER)
        cls.tplus_scheduler = _load_module("tplus_sync_scheduler_kernel", TPLUS_SCHEDULER)

        sys.path.insert(0, str(DOC_ROOT))
        from app.pipelines.sync_schedule import next_full_sync_due

        sys.path[:0] = [str(TPLUS_ROOT), str(TPLUS_SERVICE_ROOT)]
        from tplus_datahub.jobs.worker_loop import next_scheduled_full_due

        cls.doc_legacy_due = next_full_sync_due
        cls.tplus_legacy_due = next_scheduled_full_due

    @classmethod
    def tearDownClass(cls) -> None:
        for path in (str(DOC_ROOT), str(TPLUS_ROOT), str(TPLUS_SERVICE_ROOT)):
            if path in sys.path:
                sys.path.remove(path)

    def test_worker_scheduler_copies_are_byte_identical(self):
        self.assertEqual(DOC_SCHEDULER.read_bytes(), TPLUS_SCHEDULER.read_bytes())

    def test_normalize_mode_accepts_only_explicit_modes(self):
        self.assertEqual("legacy", self.scheduler.normalize_mode(None))
        self.assertEqual("shadow", self.scheduler.normalize_mode(" SHADOW "))
        self.assertEqual("active", self.scheduler.normalize_mode("active"))
        self.assertEqual("legacy", self.scheduler.normalize_mode("unknown"))

    def test_literal_schedule_cases(self):
        anchor = "02:00"
        cases = (
            ("first run", NOW, None, True, 86400, anchor, NOW, True),
            (
                "crosses day",
                datetime(2026, 8, 12, 18, 30, tzinfo=timezone.utc),
                datetime(2026, 8, 12, 17, 30, tzinfo=timezone.utc),
                True,
                86400,
                anchor,
                datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc),
                True,
            ),
            (
                "36 hour interval",
                datetime(2026, 8, 14, 5, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
                True,
                129600,
                anchor,
                datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc),
                True,
            ),
            (
                "anchor after last full",
                datetime(2026, 8, 12, 19, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 12, 17, 30, tzinfo=timezone.utc),
                True,
                86400,
                anchor,
                datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc),
                True,
            ),
            (
                "anchor before last full",
                datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 12, 19, 0, tzinfo=timezone.utc),
                True,
                86400,
                anchor,
                datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc),
                False,
            ),
            (
                "last full equals anchor",
                datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc),
                True,
                86400,
                anchor,
                datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc),
                True,
            ),
            (
                "naive last full means utc",
                datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 12, 12, 0),
                True,
                86400,
                "",
                datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
                True,
            ),
            (
                "disabled",
                datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
                False,
                86400,
                "",
                datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
                False,
            ),
            (
                "due exactly now",
                datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 13, 11, 0, tzinfo=timezone.utc),
                True,
                3600,
                "",
                datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
                True,
            ),
            (
                "no anchor uses doc semantics",
                NOW,
                LAST_FULL,
                True,
                86400,
                "",
                LAST_FULL + timedelta(days=1),
                False,
            ),
        )
        for name, now, last_full, enabled, interval, anchor_time, due, run_full in cases:
            with self.subTest(name=name):
                decision = self.scheduler.decide(now, last_full, enabled, interval, anchor_time)
                self.assertEqual(due, decision.due)
                self.assertEqual(run_full, decision.run_full)
                self.assertEqual(max(int((due - now).total_seconds()), 0), decision.wait_seconds)
                self.assertEqual(timezone.utc, decision.due.tzinfo)

    def test_no_anchor_uses_doc_semantics(self):
        decision = self.scheduler.decide(NOW, LAST_FULL, True, 86400, "")
        self.assertFalse(decision.run_full)
        self.assertEqual(LAST_FULL + timedelta(days=1), decision.due)

    def test_anchor_cases_match_both_current_production_functions(self):
        cases = (
            (datetime(2026, 8, 12, 19, tzinfo=timezone.utc), datetime(2026, 8, 12, 17, 30, tzinfo=timezone.utc), 86400, "02:00"),
            (datetime(2026, 8, 13, 1, tzinfo=timezone.utc), datetime(2026, 8, 12, 19, tzinfo=timezone.utc), 86400, "02:00"),
            (datetime(2026, 8, 14, 5, tzinfo=timezone.utc), datetime(2026, 8, 12, 12, tzinfo=timezone.utc), 129600, "02:00"),
        )
        for now, last_full, interval, anchor_time in cases:
            with self.subTest(now=now, last_full=last_full, interval=interval):
                candidate = self.scheduler.decide(now, last_full, True, interval, anchor_time)
                doc_due = type(self).doc_legacy_due(now, last_full, interval, anchor_time)
                tplus_due = type(self).tplus_legacy_due(now, last_full, interval, anchor_time)
                self.assertEqual(doc_due, tplus_due)
                self.assertEqual(doc_due, candidate.due)
                self.assertEqual(doc_due <= now, candidate.run_full)

    def test_target_moved_earlier_requires_more_than_thirty_seconds(self):
        planned = datetime(2026, 8, 13, 12, 10, tzinfo=timezone.utc)
        self.assertFalse(self.scheduler.target_moved_earlier(planned, planned - timedelta(seconds=30)))
        self.assertTrue(self.scheduler.target_moved_earlier(planned, planned - timedelta(seconds=31)))

    def test_shadow_payload_is_json_safe_and_compares_literal_values(self):
        legacy = self.scheduler.ScheduleDecision(NOW, True, 0)
        candidate = self.scheduler.ScheduleDecision(NOW, True, 0)
        payload = self.scheduler.shadow_payload(sampled_at=NOW, legacy=legacy, candidate=candidate)
        self.assertEqual({"decision_match": True, "due_delta_seconds": 0.0}, {
            key: payload[key] for key in ("decision_match", "due_delta_seconds")
        })
        self.assertEqual("2026-08-13T11:00:00+00:00", payload["sampled_at"])
        self.assertEqual("2026-08-13T11:00:00+00:00", payload["legacy"]["due"])
        self.assertEqual("2026-08-13T11:00:00+00:00", payload["candidate"]["due"])
        json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
