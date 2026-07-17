# tests/test_version_ops_api.py
from __future__ import annotations
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


class BuildInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [p for p in sys.path if p != str(BACKEND_ROOT)]

    def _comps(self):
        return [
            {"component_key": "immich-server", "display_name": "Immich", "family": "third-party",
             "upstream_source": "github-release", "match_images": ["ghcr.io/immich-app/immich-server"],
             "devices": ["webdock1"], "version_pattern": None, "pin_note": None},
            {"component_key": "webdock", "display_name": "WebDock", "family": "own",
             "upstream_source": "none", "match_images": ["ghcr.io/huozao/webdock"],
             "devices": ["webdock1", "webdock2"], "version_pattern": None, "pin_note": None},
        ]

    def test_behind_when_current_below_latest(self) -> None:
        from app.routers.versions import build_inventory
        reports = [{"device": "webdock1", "image": "ghcr.io/immich-app/immich-server",
                    "tag": "v1.134.0", "extra": {}}]
        upstream = {"immich-server": {"latest_version": "1.135.3", "release_url": "http://x"}}
        inv = build_inventory(reports, self._comps(), upstream)
        comp = inv["devices"][0]["components"][0]
        self.assertEqual(comp["status"], "behind")
        self.assertEqual(inv["summary"]["behind"], 1)

    def test_own_mismatch_across_devices(self) -> None:
        from app.routers.versions import build_inventory
        reports = [
            {"device": "webdock1", "image": "ghcr.io/huozao/webdock", "tag": "sha-aaa", "extra": {}},
            {"device": "webdock2", "image": "ghcr.io/huozao/webdock", "tag": "sha-bbb", "extra": {}},
        ]
        inv = build_inventory(reports, self._comps(), {})
        statuses = {c["status"] for d in inv["devices"] for c in d["components"]}
        self.assertIn("own-mismatch", statuses)

    def test_unregistered_image_surfaces(self) -> None:
        from app.routers.versions import build_inventory
        reports = [{"device": "aliecs", "image": "some/new-service", "tag": "1.0", "extra": {}}]
        inv = build_inventory(reports, self._comps(), {})
        comp = inv["devices"][0]["components"][0]
        self.assertEqual(comp["status"], "unregistered")

    def _openclaw_comps(self):
        return [
            {"component_key": "openclaw", "display_name": "OpenClaw", "family": "third-party",
             "upstream_source": "github-release", "match_images": ["ghcr.io/anthropic/openclaw"],
             "devices": None, "version_pattern": None, "pin_note": None},
        ]

    def test_openclaw_version_sourced_from_apt_summary_extra_behind(self) -> None:
        # openclaw 容器 tag 是占位串（latest/digest），真实版本藏在同设备
        # apt-summary 行的 extra 里，key == component_key("openclaw")
        from app.routers.versions import build_inventory
        reports = [
            {"device": "aliecs", "image": "ghcr.io/anthropic/openclaw", "tag": "latest", "extra": {}},
            {"device": "aliecs", "image": "apt-summary", "tag": None,
             "extra": {"openclaw": "2026.6.5"}},
        ]
        upstream = {"openclaw": {"latest_version": "2026.7.0", "release_url": "http://x"}}
        inv = build_inventory(reports, self._openclaw_comps(), upstream)
        oc = next(c for d in inv["devices"] for c in d["components"] if c.get("key") == "openclaw")
        self.assertEqual(oc["current"], "2026.6.5")
        self.assertEqual(oc["status"], "behind")

    def test_openclaw_version_current_when_up_to_date(self) -> None:
        from app.routers.versions import build_inventory
        reports = [
            {"device": "aliecs", "image": "ghcr.io/anthropic/openclaw", "tag": "latest", "extra": {}},
            {"device": "aliecs", "image": "apt-summary", "tag": None,
             "extra": {"openclaw": "2026.7.0"}},
        ]
        upstream = {"openclaw": {"latest_version": "2026.7.0", "release_url": "http://x"}}
        inv = build_inventory(reports, self._openclaw_comps(), upstream)
        oc = next(c for d in inv["devices"] for c in d["components"] if c.get("key") == "openclaw")
        self.assertEqual(oc["current"], "2026.7.0")
        self.assertEqual(oc["status"], "current")
