from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "services" / "backend-api"
sys.path.insert(0, str(APP))

from app.routers.versions import build_inventory  # noqa: E402


class VersionDriftTest(unittest.TestCase):
    def test_drift_preserves_expected_actual_and_reason(self) -> None:
        reports = [{
            "device": "txecs",
            "image": "apt-summary",
            "tag": None,
            "digest": None,
            "reported_at": datetime.now(timezone.utc),
            "extra": {"apt": {}, "drift": {"schema": 1, "status": "FAIL", "checked_at": "now", "checks": [
                {"id": "repo", "kind": "git", "status": "FAIL", "reason": "sha-mismatch", "expected": "a", "actual": "b"}
            ]}},
        }]
        inventory = build_inventory(reports, [], {})
        self.assertEqual(inventory["summary"]["status"], "warning")
        self.assertEqual(inventory["drift"][0]["status"], "FAIL")
        self.assertEqual(inventory["drift"][0]["checks"][0]["expected"], "a")
        self.assertEqual(inventory["drift"][0]["checks"][0]["reason"], "sha-mismatch")


if __name__ == "__main__":
    unittest.main()
