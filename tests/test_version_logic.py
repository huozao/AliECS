# tests/test_version_logic.py
from __future__ import annotations
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


class VersionLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [p for p in sys.path if p != str(BACKEND_ROOT)]

    def test_normalize_strips_prefixes_suffixes(self) -> None:
        from app.routers.versions import normalize_version
        self.assertEqual(normalize_version("v1.135.3"), "1.135.3")
        self.assertEqual(normalize_version("refs/tags/v2.0.1"), "2.0.1")
        self.assertEqual(normalize_version("16.4-alpine"), "16.4")

    def test_compare_semver(self) -> None:
        from app.routers.versions import compare_versions
        self.assertEqual(compare_versions("1.135.2", "1.135.3"), -1)
        self.assertEqual(compare_versions("1.135.3", "1.135.3"), 0)
        self.assertEqual(compare_versions("1.136.0", "1.135.3"), 1)
        self.assertEqual(compare_versions("16.4", "16.10"), -1)  # 数值比较非字符串

    def test_match_component_filters_by_device(self) -> None:
        from app.routers.versions import match_component
        comps = [
            {"component_key": "pg-a", "match_images": ["postgres"], "devices": ["aliecs"]},
            {"component_key": "pg-w", "match_images": ["postgres"], "devices": ["webdock1"]},
        ]
        self.assertEqual(match_component("postgres", "aliecs", comps)["component_key"], "pg-a")
        self.assertEqual(match_component("postgres", "webdock1", comps)["component_key"], "pg-w")
        self.assertIsNone(match_component("redis", "aliecs", comps))

    def test_match_component_null_devices_matches_any(self) -> None:
        from app.routers.versions import match_component
        comps = [{"component_key": "apt", "match_images": ["apt-summary"], "devices": None}]
        self.assertEqual(match_component("apt-summary", "webdock2", comps)["component_key"], "apt")

    def test_classify_states(self) -> None:
        from app.routers.versions import classify_component
        self.assertEqual(classify_component(family="own", upstream_source="none",
                         current="sha-abc", latest=None, version_pattern=None), "own")
        self.assertEqual(classify_component(family="third-party", upstream_source="none",
                         current="16.4", latest=None, version_pattern=None), "pinned")
        self.assertEqual(classify_component(family="third-party", upstream_source="github-release",
                         current=None, latest="1.9", version_pattern=None), "stale")
        self.assertEqual(classify_component(family="third-party", upstream_source="github-release",
                         current="1.8", latest="1.9", version_pattern=None), "behind")
        self.assertEqual(classify_component(family="third-party", upstream_source="github-release",
                         current="1.9", latest="1.9", version_pattern=None), "current")
