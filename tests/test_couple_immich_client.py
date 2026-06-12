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


if __name__ == "__main__":
    unittest.main()
