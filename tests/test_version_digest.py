from __future__ import annotations
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


class DigestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [p for p in sys.path if p != str(BACKEND_ROOT)]

    def test_all_green_still_sends_short_line(self) -> None:
        from app.routers.versions import render_digest_text
        inv = {"summary": {"status": "ok", "behind": 0, "own-mismatch": 0, "unregistered": 0},
               "devices": [{"device": "aliecs", "components": [
                   {"key": "openclaw", "name": "OpenClaw", "current": "2026.6.5",
                    "latest": "2026.6.5", "status": "current", "release_url": None, "note": None}]}]}
        text = render_digest_text(inv, [])
        self.assertIn("全部最新", text)

    def test_behind_items_listed_with_versions(self) -> None:
        from app.routers.versions import render_digest_text
        inv = {"summary": {"status": "warning", "behind": 1, "own-mismatch": 0, "unregistered": 0},
               "devices": [{"device": "webdock1", "components": [
                   {"key": "immich-server", "name": "Immich", "current": "v1.134.0",
                    "latest": "1.135.3", "status": "behind",
                    "release_url": "http://x", "note": None}]}]}
        text = render_digest_text(inv, [])
        self.assertIn("Immich", text)
        self.assertIn("1.135.3", text)

    def test_stale_device_flagged(self) -> None:
        from app.routers.versions import render_digest_text
        inv = {"summary": {"status": "ok", "behind": 0, "own-mismatch": 0, "unregistered": 0},
               "devices": []}
        text = render_digest_text(inv, ["webdock2"])
        self.assertIn("webdock2", text)
        self.assertIn("未上报", text)
