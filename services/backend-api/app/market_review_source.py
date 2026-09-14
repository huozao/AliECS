"""Read-only adapter for the devbox review archive.

Authentication and annotation writes remain on txecs.  This module never
accepts a URL, SQL statement, or file path from a browser request.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class ReviewSourceUnavailable(RuntimeError):
    pass


class ReviewSourceInvalid(RuntimeError):
    pass


class LocalReviewSource:
    def __init__(self, *, url: str | None = None, token: str | None = None,
                 opener=urllib.request.urlopen, connect_timeout: float = 2.0,
                 total_timeout: float = 8.0, max_bytes: int = 8 * 1024 * 1024):
        self.base_url = (url or os.getenv("MARKET_REVIEW_ARCHIVE_URL", "")).rstrip("/")
        self.token = token if token is not None else os.getenv("MARKET_REVIEW_ARCHIVE_TOKEN", "")
        self.opener = opener
        self.connect_timeout = connect_timeout
        self.total_timeout = total_timeout
        self.max_bytes = max_bytes
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("MARKET_REVIEW_ARCHIVE_URL must be an HTTP(S) URL")
        if not self.token:
            raise ValueError("MARKET_REVIEW_ARCHIVE_TOKEN is required")

    def get(self, path: str, query: str = "") -> dict[str, Any]:
        allowed = {"/health", "/latest", "/realtime", "/events/index", "/coverage"}
        if path not in allowed and not (path.startswith("/events/") and path.endswith("/detail")):
            raise ValueError("unsupported review source path")
        request = urllib.request.Request(
            f"{self.base_url}/internal/review/v1{path}{query}",
            headers={"X-Review-Archive-Token": self.token, "Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=self.total_timeout) as response:
                if response.status != 200:
                    raise ReviewSourceUnavailable(f"source returned {response.status}")
                payload = response.read(self.max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReviewSourceUnavailable("review source unavailable") from exc
        if len(payload) > self.max_bytes:
            raise ReviewSourceInvalid("review source response exceeds limit")
        try:
            body = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewSourceInvalid("review source returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ReviewSourceInvalid("review source returned a non-object")
        if path == "/realtime" and len(json.dumps(body, separators=(",", ":"))) > self.max_bytes:
            raise ReviewSourceInvalid("review source response exceeds limit")
        return body
