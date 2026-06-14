from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
JPEG_BYTES = b"\xff\xd8\xff\xe0photo"


def load_main():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import main

    return main


def upload_file(name: str = "x.jpeg") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(JPEG_BYTES),
        filename=name,
        headers=Headers({"content-type": "image/jpeg"}),
    )


class CoupleLocalPhotoStorageTests(unittest.TestCase):
    def test_local_storage_defaults_to_persistent_container_dir(self) -> None:
        main = load_main()

        with patch.dict(os.environ, {}, clear=False), patch.object(main.Path, "mkdir", return_value=None):
            os.environ.pop("LOCAL_UPLOAD_DIR", None)
            store = main.LocalPhotoStorage()

        self.assertEqual(Path("/app/uploads"), store.base_dir)

    def test_local_storage_persists_under_configured_dir(self) -> None:
        main = load_main()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"LOCAL_UPLOAD_DIR": str(Path(tmp) / "uploads")}, clear=False):
                store = main.LocalPhotoStorage()
                result = asyncio.run(store.save(upload_file()))

            self.assertTrue(result["display_url"].startswith("/uploads/"))
            saved = Path(result["original_storage_url"])
            self.assertTrue(saved.exists())
            self.assertEqual(JPEG_BYTES, saved.read_bytes())

    def test_local_storage_serves_saved_file(self) -> None:
        main = load_main()
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp) / "uploads"
            with patch.dict(os.environ, {"LOCAL_UPLOAD_DIR": str(upload_dir)}, clear=False):
                saved = asyncio.run(main.LocalPhotoStorage().save(upload_file("served.jpeg")))
                response = TestClient(main.app).get(saved["display_url"])

            self.assertEqual(200, response.status_code)
            self.assertEqual(JPEG_BYTES, response.content)
            self.assertTrue(response.headers["content-type"].startswith("image/jpeg"))

    def test_webdock_unavailable_falls_back_to_local_storage(self) -> None:
        main = load_main()

        def down(_request, timeout):
            raise urllib.error.URLError("webdock down")

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp) / "uploads"
            env = {
                "STORAGE_DRIVER": "webdock",
                "WEBDOCK_PHOTO_API_TOKEN": "secret-token",
                "LOCAL_UPLOAD_DIR": str(upload_dir),
            }
            with patch.dict(os.environ, env, clear=False), patch.object(main.urllib.request, "urlopen", down):
                result = asyncio.run(main.photo_storage().save(upload_file("fallback.jpeg")))

            self.assertEqual("local", result["storage_driver"])
            saved = Path(result["original_storage_url"])
            self.assertTrue(saved.exists())


if __name__ == "__main__":
    unittest.main()
