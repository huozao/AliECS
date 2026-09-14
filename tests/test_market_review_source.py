import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "backend-api"))
from app.market_review_source import LocalReviewSource, ReviewSourceUnavailable


class Response:
    status = 200
    def __init__(self, body): self.body = body
    def read(self, _limit=None): return self.body
    def __enter__(self): return self
    def __exit__(self, *_args): return False


def test_source_sends_service_token_and_rejects_browser_selected_path():
    seen = []
    source = LocalReviewSource(url="https://devbox.example.test:18210", token="secret",
                               opener=lambda request, timeout: (seen.append((request.full_url, request.get_header("X-review-archive-token"), timeout)) or Response(b'{"ok":true}')))
    assert source.get("/latest") == {"ok": True}
    assert seen[0][0].endswith("/internal/review/v1/latest")
    assert seen[0][1] == "secret" and seen[0][2] == 8.0
    with pytest.raises(ValueError): source.get("/../etc/passwd")


def test_source_errors_are_generic_and_bound_payload():
    def fail(*_args, **_kwargs): raise urllib.error.URLError("private address")
    source = LocalReviewSource(url="https://devbox.example.test", token="secret", opener=fail)
    with pytest.raises(ReviewSourceUnavailable, match="source unavailable"):
        source.get("/realtime")
    source = LocalReviewSource(url="https://devbox.example.test", token="secret",
                               opener=lambda *_args, **_kwargs: Response(b"x" * 10))
    with pytest.raises(Exception): source.get("/latest")


def test_source_rejects_invalid_status_and_json():
    class Bad(Response): status = 503
    source = LocalReviewSource(url="https://devbox.example.test", token="secret",
                               opener=lambda *_args, **_kwargs: Bad(b"{}"))
    with pytest.raises(ReviewSourceUnavailable): source.get("/coverage")
    source = LocalReviewSource(url="https://devbox.example.test", token="secret",
                               opener=lambda *_args, **_kwargs: Response(b"[]"))
    with pytest.raises(Exception): source.get("/coverage")
