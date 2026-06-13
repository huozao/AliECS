from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


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


class HealthzDiskTests(unittest.TestCase):
    def test_healthz_reports_upload_disk_usage(self) -> None:
        main = load_main()
        client = TestClient(main.app)

        usage = main.shutil._ntuple_diskusage(total=1000, used=900, free=100)
        with patch.object(main.shutil, "disk_usage", return_value=usage):
            os.environ.pop("DATABASE_URL", None)
            resp = client.get("/healthz")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("upload_disk", body)
        self.assertEqual(body["upload_disk"]["percent"], 90.0)
        self.assertIn("path", body["upload_disk"])

    def test_healthz_ok_when_upload_dir_missing(self) -> None:
        # Regression: /healthz must not 500 when the upload dir does not exist
        # (the dir is created lazily by LocalPhotoStorage, not at healthcheck time).
        main = load_main()
        client = TestClient(main.app, raise_server_exceptions=False)

        missing = os.path.join(tempfile.gettempdir(), f"aliecs-missing-{uuid.uuid4().hex}")
        with patch.dict(os.environ, {"LOCAL_UPLOAD_DIR": missing}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            resp = client.get("/healthz")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("upload_disk", body)
        self.assertFalse(body["upload_disk"]["available"])


if __name__ == "__main__":
    unittest.main()
