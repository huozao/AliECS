from __future__ import annotations

import asyncio
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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
    def __init__(self, status: int = 200):
        self.status = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return b""


class OssClientSigningTests(unittest.TestCase):
    def test_canonical_resource_and_headers(self) -> None:
        main = load_main()
        from app.oss_client import OssClient, OssConfig

        config = OssConfig(
            endpoint="oss-cn-hangzhou.aliyuncs.com",
            bucket="aliecs-photos",
            access_key_id="AKIDEXAMPLE",
            access_key_secret="secret",
        )
        client = OssClient(config)

        headers = client._signed_headers("PUT", "couple/abc.png", content_type="image/png")
        self.assertIn("Authorization", headers)
        self.assertTrue(headers["Authorization"].startswith("OSS AKIDEXAMPLE:"))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertIn("Date", headers)

    def test_object_url(self) -> None:
        main = load_main()
        from app.oss_client import OssClient, OssConfig

        config = OssConfig(
            endpoint="oss-cn-hangzhou.aliyuncs.com",
            bucket="aliecs-photos",
            access_key_id="AKIDEXAMPLE",
            access_key_secret="secret",
        )
        client = OssClient(config)
        self.assertEqual(
            client.object_url("couple/abc.png"),
            "https://aliecs-photos.oss-cn-hangzhou.aliyuncs.com/couple/abc.png",
        )


class OssPhotoStorageTests(unittest.TestCase):
    def test_save_uploads_and_returns_oss_urls(self) -> None:
        main = load_main()

        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request)
            return FakeResponse(status=200)

        env = {
            "STORAGE_DRIVER": "oss",
            "OSS_ENDPOINT": "oss-cn-hangzhou.aliyuncs.com",
            "OSS_BUCKET": "aliecs-photos",
            "OSS_ACCESS_KEY_ID": "AKIDEXAMPLE",
            "OSS_ACCESS_KEY_SECRET": "secret",
            "MAX_UPLOAD_MB": "15",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(main.urllib.request, "urlopen", fake_urlopen):
            storage = main.photo_storage()
            self.assertEqual(storage.driver, "oss")

            upload = UploadFile(
                file=io.BytesIO(b"\x89PNG\r\n\x1a\nphoto"),
                filename="memory.png",
                headers=Headers({"content-type": "image/png"}),
            )
            result = asyncio.run(storage.save(upload))

        self.assertEqual(result["storage_driver"], "oss")
        self.assertTrue(result["display_url"].startswith("https://aliecs-photos.oss-cn-hangzhou.aliyuncs.com/"))
        self.assertEqual(result["display_url"], result["thumbnail_url"])
        self.assertTrue(len(calls) >= 1)
        self.assertEqual(calls[0].get_method(), "PUT")

    def test_save_without_config_raises_501(self) -> None:
        main = load_main()
        env = {"STORAGE_DRIVER": "oss"}
        for key in ("OSS_ENDPOINT", "OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"):
            env[key] = ""
        with patch.dict(os.environ, env, clear=False):
            storage = main.photo_storage()
            upload = UploadFile(
                file=io.BytesIO(b"\x89PNG\r\n\x1a\nphoto"),
                filename="memory.png",
                headers=Headers({"content-type": "image/png"}),
            )
            with self.assertRaises(main.HTTPException) as ctx:
                asyncio.run(storage.save(upload))

        self.assertEqual(ctx.exception.status_code, 501)


if __name__ == "__main__":
    unittest.main()
