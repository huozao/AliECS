from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi import HTTPException


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


def test_anniversary_next_occurrence_repeat_modes():
    main = load_main()
    today = date(2026, 6, 5)

    assert main._next_anniversary_occurrence(date(2024, 6, 20), "yearly", today) == date(2026, 6, 20)
    assert main._next_anniversary_occurrence(date(2024, 6, 1), "yearly", today) == date(2027, 6, 1)
    assert main._next_anniversary_occurrence(date(2024, 1, 31), "monthly", date(2026, 2, 28)) == date(2026, 2, 28)
    assert main._next_anniversary_occurrence(date(2026, 6, 4), "none", today) is None
    assert main._next_anniversary_occurrence(date(2026, 6, 6), "none", today) == date(2026, 6, 6)


def test_normalize_tags_deduplicates_and_trims():
    main = load_main()

    assert main._normalize_tags([" 旅行 ", "", "旅行", "晚风"]) == ["旅行", "晚风"]


def test_photo_upload_validation_rejects_fake_image(monkeypatch):
    main = load_main()
    monkeypatch.setenv("MAX_UPLOAD_MB", "15")

    with pytest.raises(HTTPException) as exc:
        main._validate_photo_upload("fake.jpg", "image/jpeg", b"MZnot-an-image")

    assert exc.value.status_code == 400


def test_photo_upload_validation_accepts_matching_png(monkeypatch):
    main = load_main()
    old = os.environ.get("MAX_UPLOAD_MB")
    monkeypatch.setenv("MAX_UPLOAD_MB", "15")

    ext, mime = main._validate_photo_upload("ok.png", "image/png", b"\x89PNG\r\n\x1a\nsample")

    assert (ext, mime) == (".png", "image/png")
    if old is not None:
        monkeypatch.setenv("MAX_UPLOAD_MB", old)
