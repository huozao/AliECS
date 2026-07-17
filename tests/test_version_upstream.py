from __future__ import annotations
import io, json, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


def fake_opener(payload):
    def _open(req, timeout=None):
        return io.BytesIO(json.dumps(payload).encode())
    return _open


class UpstreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [p for p in sys.path if p != str(BACKEND_ROOT)]

    def test_github_latest_parses_tag_and_url(self) -> None:
        from app.routers.versions import fetch_github_latest
        op = fake_opener({"tag_name": "v1.135.3", "html_url": "https://github.com/x/releases/v1.135.3"})
        tag, url = fetch_github_latest("immich-app/immich", opener=op)
        self.assertEqual(tag, "v1.135.3")
        self.assertIn("releases", url)

    def test_dockerhub_filters_by_pattern_and_picks_max(self) -> None:
        from app.routers.versions import fetch_dockerhub_latest
        op = fake_opener({"results": [
            {"name": "16.4"}, {"name": "16.10"}, {"name": "17.2"}, {"name": "latest"},
        ]})
        tag, url = fetch_dockerhub_latest("library/postgres", r"^16\.", opener=op)
        self.assertEqual(tag, "16.10")  # 锁 16 大版本，选 16.x 内最大

    def test_github_failure_returns_none(self) -> None:
        from app.routers.versions import fetch_github_latest
        def boom(req, timeout=None): raise OSError("network")
        tag, url = fetch_github_latest("x/y", opener=boom)
        self.assertIsNone(tag)
