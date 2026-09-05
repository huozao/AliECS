from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_immich_client():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app.immich_client import ImmichAsset, ImmichClient, ImmichConfig

    return ImmichAsset, ImmichClient, ImmichConfig


class ImmichClientTests(unittest.TestCase):
    def test_current_user_uses_api_key_route(self) -> None:
        _, ImmichClient, ImmichConfig = load_immich_client()
        response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=None); response.read.return_value = b'{"id":"user-1"}'
        with patch("app.immich_client.urllib.request.urlopen", return_value=response) as urlopen:
            result = ImmichClient(ImmichConfig(True, "https://immich.example", "secret")).current_user()
        self.assertEqual("user-1", result["id"])
        self.assertEqual("https://immich.example/api/users/me", urlopen.call_args.args[0].full_url)

    def test_add_assets_to_album_uses_idempotent_ids(self) -> None:
        _, ImmichClient, ImmichConfig = load_immich_client()
        response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=None); response.read.return_value = b""
        with patch("app.immich_client.urllib.request.urlopen", return_value=response) as urlopen:
            ImmichClient(ImmichConfig(True, "https://immich.example", "secret")).add_assets_to_album("album/1", ["a", "a", "b"])
        request = urlopen.call_args.args[0]
        self.assertEqual("PUT", request.get_method())
        self.assertEqual("https://immich.example/api/albums/album%2F1/assets", request.full_url)
        self.assertEqual({"ids": ["a", "b"]}, json.loads(request.data.decode()))
    def test_disabled_client_reports_disabled(self) -> None:
        _, ImmichClient, ImmichConfig = load_immich_client()
        client = ImmichClient(ImmichConfig(enabled=False, base_url="", api_key="", timeout_seconds=5))

        self.assertEqual({"enabled": False, "ok": False, "detail": "Immich integration disabled"}, client.status())

    def test_ping_uses_api_key_and_normalizes_success(self) -> None:
        _, ImmichClient, ImmichConfig = load_immich_client()
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b'{"res":"pong"}'

        with patch("app.immich_client.urllib.request.urlopen", return_value=response) as urlopen:
            client = ImmichClient(
                ImmichConfig(
                    enabled=True,
                    base_url="https://immich.example",
                    api_key="secret",
                    timeout_seconds=5,
                )
            )

            self.assertEqual(True, client.ping())

        request = urlopen.call_args.args[0]
        self.assertEqual("https://immich.example/api/server/ping", request.full_url)
        self.assertEqual("secret", request.headers["X-api-key"])

    def test_get_asset_normalizes_response(self) -> None:
        ImmichAsset, ImmichClient, ImmichConfig = load_immich_client()
        payload = {
            "id": "asset-1",
            "originalFileName": "a.jpg",
            "fileCreatedAt": "2026-03-20T12:00:00Z",
            "exifInfo": {"latitude": 30.1, "longitude": 120.2},
        }
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = json.dumps(payload).encode("utf-8")

        with patch("app.immich_client.urllib.request.urlopen", return_value=response):
            client = ImmichClient(
                ImmichConfig(
                    enabled=True,
                    base_url="https://immich.example/",
                    api_key="secret",
                    timeout_seconds=5,
                )
            )
            asset = client.get_asset("asset-1")

        self.assertIsInstance(asset, ImmichAsset)
        self.assertEqual("asset-1", asset.asset_id)
        self.assertEqual("a.jpg", asset.original_filename)
        self.assertEqual("2026-03-20T12:00:00Z", asset.taken_at)
        self.assertEqual(30.1, asset.latitude)
        self.assertEqual(120.2, asset.longitude)

    def test_search_assets_posts_metadata_query_and_normalizes_results(self) -> None:
        ImmichAsset, ImmichClient, ImmichConfig = load_immich_client()
        payload = {
            "assets": {
                "items": [
                    {
                        "id": "asset-2",
                        "originalFileName": "b.jpg",
                        "fileCreatedAt": "2026-04-01T08:00:00Z",
                        "exifInfo": {"latitude": 31.2, "longitude": 121.4},
                    }
                ]
            }
        }
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = json.dumps(payload).encode("utf-8")

        with patch("app.immich_client.urllib.request.urlopen", return_value=response) as urlopen:
            client = ImmichClient(
                ImmichConfig(
                    enabled=True,
                    base_url="https://immich.example/",
                    api_key="secret",
                    timeout_seconds=5,
                )
            )
            assets = client.search_assets(query="春分", taken_after="2026-03-01", taken_before="2026-04-30", page=2)

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("https://immich.example/api/search/metadata", request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual("春分", body["query"])
        self.assertEqual("2026-03-01", body["takenAfter"])
        self.assertEqual("2026-04-30", body["takenBefore"])
        self.assertEqual(2, body["page"])
        self.assertEqual([ImmichAsset("asset-2", "b.jpg", "2026-04-01T08:00:00Z", 31.2, 121.4)], assets)

    def test_search_assets_page_supports_album_filter_and_next_page(self) -> None:
        _, ImmichClient, ImmichConfig = load_immich_client()
        payload = {"assets": {"total": 30, "count": 30, "nextPage": "2", "items": []}}
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = json.dumps(payload).encode("utf-8")

        with patch("app.immich_client.urllib.request.urlopen", return_value=response) as urlopen:
            client = ImmichClient(ImmichConfig(True, "https://immich.example", "secret"))
            assets, total, next_page = client.search_assets_page(page=1, album_ids=["album-1", "album-1"])

        request = urlopen.call_args.args[0]
        self.assertEqual([], assets)
        self.assertEqual(30, total)
        self.assertEqual(2, next_page)
        self.assertEqual(["album-1"], json.loads(request.data.decode())["albumIds"])


if __name__ == "__main__":
    unittest.main()
