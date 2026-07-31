from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_nav_impact.py"


def check(message: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--message", message],
        text=True,
        capture_output=True,
        check=False,
    )


class NavImpactTest(unittest.TestCase):
    def test_missing_is_red(self) -> None:
        self.assertEqual(check("ordinary change").returncode, 1)

    def test_updated_is_green(self) -> None:
        self.assertEqual(check("Nav-Impact: updated").returncode, 0)

    def test_none_requires_reason(self) -> None:
        self.assertEqual(check("Nav-Impact: none").returncode, 1)
        self.assertEqual(
            check("Nav-Impact: none\nNav-Impact-Reason: navigation is unchanged").returncode,
            0,
        )


if __name__ == "__main__":
    unittest.main()

