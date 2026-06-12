from __future__ import annotations

from dataclasses import dataclass
import json
import os
import urllib.error
import urllib.parse
import urllib.request


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

    def get_asset(self, asset_id: str) -> ImmichAsset:
        payload = self._request_json(f"/api/assets/{urllib.parse.quote(asset_id, safe='')}")
        exif = payload.get("exifInfo") or {}
        return ImmichAsset(
            asset_id=str(payload.get("id") or asset_id),
            original_filename=payload.get("originalFileName"),
            taken_at=payload.get("fileCreatedAt") or payload.get("localDateTime"),
            latitude=exif.get("latitude"),
            longitude=exif.get("longitude"),
        )

    def _request_json(self, path: str) -> dict:
        if not self.config.enabled:
            raise ValueError("Immich integration disabled")
        if not self.config.base_url:
            raise ValueError("IMMICH_BASE_URL is required")
        if not self.config.api_key:
            raise ValueError("IMMICH_API_KEY is required")
        request = urllib.request.Request(
            f"{self.config.base_url}{path}",
            headers={"x-api-key": self.config.api_key, "accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
            raw = response.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))
