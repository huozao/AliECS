from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_main():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import main

    return main


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json", status: int = 200):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


class WebDockPhotoStorageTests(unittest.TestCase):
    def test_uploads_validated_image_and_returns_proxy_url(self) -> None:
        main = load_main()
        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(
                json.dumps({"key": "abc123.png", "content_type": "image/png", "size": 12}).encode("utf-8")
            )

        env = {
            "WEBDOCK_PHOTO_BASE_URL": "http://webdock.local",
            "WEBDOCK_PHOTO_API_TOKEN": "secret-token",
            "MAX_UPLOAD_MB": "15",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(main.urllib.request, "urlopen", fake_urlopen):
            os.environ.pop("APP_BASE_URL", None)
            upload = UploadFile(
                file=io.BytesIO(b"\x89PNG\r\n\x1a\nphoto"),
                filename="memory.png",
                headers=Headers({"content-type": "image/png"}),
            )

            saved = asyncio.run(main.WebDockPhotoStorage().save(upload))

        request, timeout = calls[0]
        self.assertEqual("http://webdock.local/storage/photos", request.full_url)
        self.assertEqual("Bearer secret-token", request.headers["Authorization"])
        self.assertTrue(request.headers["Content-type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b"\x89PNG\r\n\x1a\nphoto", request.data)
        self.assertEqual(30, timeout)
        self.assertEqual(
            {
                "original_storage_url": "webdock:abc123.png",
                "display_url": "/api/v1/photos/content/abc123.png",
                "thumbnail_url": "/api/v1/photos/content/abc123.png",
                "storage_driver": "webdock",
            },
            saved,
        )

    def test_proxy_streams_bytes_without_login(self) -> None:
        main = load_main()

        def fake_urlopen(request, timeout):
            self.assertEqual("http://webdock.local/storage/photos/abc123.png", request.full_url)
            self.assertEqual("Bearer secret-token", request.headers["Authorization"])
            self.assertEqual(30, timeout)
            return FakeResponse(b"\x89PNG\r\n\x1a\nphoto", "image/png")

        env = {
            "WEBDOCK_PHOTO_BASE_URL": "http://webdock.local",
            "WEBDOCK_PHOTO_API_TOKEN": "secret-token",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(main.urllib.request, "urlopen", fake_urlopen):
            response = TestClient(main.app).get("/v1/photos/content/abc123.png")

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"\x89PNG\r\n\x1a\nphoto", response.content)
        self.assertEqual("image/png", response.headers["content-type"])

    def test_requires_token(self) -> None:
        main = load_main()
        with patch.dict(os.environ, {"WEBDOCK_PHOTO_BASE_URL": "http://webdock.local"}, clear=False):
            os.environ.pop("WEBDOCK_PHOTO_API_TOKEN", None)
            os.environ.pop("WEB_DOCK_API_TOKEN", None)
            with self.assertRaises(HTTPException):
                main._webdock_photo_request("GET", "abc123.png")
