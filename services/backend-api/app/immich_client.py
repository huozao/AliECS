from __future__ import annotations

from dataclasses import dataclass
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone


@dataclass(frozen=True)
class ImmichConfig:
    enabled: bool
    base_url: str
    api_key: str
    timeout_seconds: int = 20


@dataclass(frozen=True)
class ImmichAsset:
    asset_id: str
    original_filename: str | None
    taken_at: str | None
    latitude: float | None
    longitude: float | None
    thumbnail_url: str | None = None


def load_immich_config() -> ImmichConfig:
    enabled = os.getenv("IMMICH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    timeout_raw = os.getenv("IMMICH_TIMEOUT_SECONDS", "20").strip()
    try:
        timeout_seconds = int(timeout_raw)
    except ValueError:
        timeout_seconds = 20
    return ImmichConfig(
        enabled=enabled,
        base_url=os.getenv("IMMICH_BASE_URL", "").strip().rstrip("/"),
        api_key=os.getenv("IMMICH_API_KEY", "").strip(),
        timeout_seconds=max(1, timeout_seconds),
    )


class ImmichClient:
    def __init__(self, config: ImmichConfig | None = None) -> None:
        self.config = config or load_immich_config()

    def status(self) -> dict[str, object]:
        if not self.config.enabled:
            return {"enabled": False, "ok": False, "detail": "Immich integration disabled"}
        if not self.config.base_url:
            return {"enabled": True, "ok": False, "detail": "IMMICH_BASE_URL is required"}
        if not self.config.api_key:
            return {"enabled": True, "ok": False, "detail": "IMMICH_API_KEY is required"}
        return {"enabled": True, "ok": self.ping(), "detail": "ok"}

    def ping(self) -> bool:
        if not self.config.enabled or not self.config.base_url or not self.config.api_key:
            return False
        try:
            self._request_json("/api/server/ping")
        except (TimeoutError, ValueError, urllib.error.URLError):
            return False
        return True

    def current_user(self) -> dict[str, object]:
        # Immich exposes the API-key-authenticated identity at /api/users/me.
        # /api/auth/user is a browser-session route and rejects valid API keys.
        return self._request_json("/api/users/me")

    def get_asset(self, asset_id: str) -> ImmichAsset:
        payload = self._request_json(f"/api/assets/{urllib.parse.quote(asset_id, safe='')}")
        return self._asset_from_payload(payload, fallback_id=asset_id)

    def search_assets(
        self,
        query: str | None = None,
        taken_after: str | None = None,
        taken_before: str | None = None,
        page: int = 1,
    ) -> list[ImmichAsset]:
        payload: dict[str, object] = {"page": max(1, int(page)), "size": 30}
        if query:
            payload["query"] = query
        if taken_after:
            payload["takenAfter"] = taken_after
        if taken_before:
            payload["takenBefore"] = taken_before
        data = self._request_json("/api/search/metadata", method="POST", payload=payload)
        raw_items = data.get("items")
        assets = data.get("assets")
        if raw_items is None and isinstance(assets, dict):
            raw_items = assets.get("items")
        if raw_items is None:
            raw_items = data.get("results") or []
        return [self._asset_from_payload(item) for item in raw_items if isinstance(item, dict)]

    def get_thumbnail(self, asset_id: str) -> tuple[bytes, str]:
        return self._request_bytes(f"/api/assets/{urllib.parse.quote(asset_id, safe='')}/thumbnail?size=thumbnail")

    def list_albums(self) -> list[dict[str, object]]:
        payload = self._request_json("/api/albums")
        return payload if isinstance(payload, list) else payload.get("items", [])

    def add_assets_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        if not asset_ids:
            return
        self._request_json(
            f"/api/albums/{urllib.parse.quote(album_id, safe='')}/assets",
            method="PUT",
            payload={"ids": list(dict.fromkeys(asset_ids))},
        )

    def upload_asset(self, filename: str, content_type: str, content: bytes) -> dict[str, object]:
        boundary = f"----couple-immich-{uuid.uuid4().hex}"
        safe_name = (filename or "upload").replace("\\", "_").replace('"', "_")
        fields = {
            "deviceAssetId": uuid.uuid4().hex,
            "deviceId": "couple-memory",
            "fileCreatedAt": datetime.now(timezone.utc).isoformat(),
            "fileModifiedAt": datetime.now(timezone.utc).isoformat(),
            "isFavorite": "false",
        }
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="assetData"; filename="{safe_name}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
        )
        chunks.extend([content, f"\r\n--{boundary}--\r\n".encode()])
        raw, _ = self._request(
            "/api/assets",
            method="POST",
            payload=None,
            accept="application/json",
            raw_data=b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _asset_from_payload(self, payload: dict, fallback_id: str | None = None) -> ImmichAsset:
        exif = payload.get("exifInfo") or {}
        return ImmichAsset(
            asset_id=str(payload.get("id") or payload.get("assetId") or fallback_id or ""),
            original_filename=payload.get("originalFileName") or payload.get("originalPath"),
            taken_at=payload.get("fileCreatedAt") or payload.get("localDateTime") or payload.get("createdAt"),
            latitude=exif.get("latitude") or payload.get("latitude"),
            longitude=exif.get("longitude") or payload.get("longitude"),
        )

    def _request_json(self, path: str, method: str = "GET", payload: dict[str, object] | None = None) -> dict:
        raw, _content_type = self._request(path, method=method, payload=payload, accept="application/json")
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _request_bytes(self, path: str) -> tuple[bytes, str]:
        return self._request(path, method="GET", payload=None, accept="*/*")

    def _request(
        self,
        path: str,
        method: str,
        payload: dict[str, object] | None,
        accept: str,
        raw_data: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[bytes, str]:
        if not self.config.enabled:
            raise ValueError("Immich integration disabled")
        if not self.config.base_url:
            raise ValueError("IMMICH_BASE_URL is required")
        if not self.config.api_key:
            raise ValueError("IMMICH_API_KEY is required")
        data = raw_data
        headers = {"x-api-key": self.config.api_key, "accept": accept}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["content-type"] = content_type
        base_url = self.config.base_url.rstrip("/")
        request = urllib.request.Request(
            f"{base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
            raw = response.read()
            content_type = response.headers.get("content-type", "application/octet-stream")
        return raw, content_type
