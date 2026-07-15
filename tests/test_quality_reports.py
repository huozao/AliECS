from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))

from app.main import app
from app.quality_storage import StorageBackend, StorageError, WebDavStorage


def _backend() -> StorageBackend:
    return StorageBackend(1, "nutstore_qc_01", "nutstore_webdav", "QC 01", "QUALITY_WEBDAV_01", "quality-reports")


def test_quality_report_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/v1/quality-reports" in paths
    assert "/v1/quality-reports/{report_id}/files" in paths
    assert "/v1/quality-reports/files/{file_id}/download" in paths
    assert "/v1/quality-reports/storage/health-check" in paths


def test_webdav_requires_secret_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUALITY_WEBDAV_01_USER", raising=False)
    monkeypatch.delenv("QUALITY_WEBDAV_01_PASS", raising=False)
    with pytest.raises(StorageError, match="QUALITY_WEBDAV_01"):
        WebDavStorage(_backend())


def test_webdav_url_quotes_path_but_keeps_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUALITY_WEBDAV_01_USER", "user")
    monkeypatch.setenv("QUALITY_WEBDAV_01_PASS", "pass")
    storage = WebDavStorage(_backend())
    assert storage._url("quality-reports/产品 A/report.pdf").endswith(
        "quality-reports/%E4%BA%A7%E5%93%81%20A/report.pdf"
    )
