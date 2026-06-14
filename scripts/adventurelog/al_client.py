from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any

import httpx


REF_RE = re.compile(r"aliecs-memory:\d+")


class AdventureLogClient:
    """Thin AdventureLog REST client.

    TODO(AdventureLog v0.12.1核对): 创建 adventure、查询列表、Immich asset 关联端点和字段名
    需在 Phase D 按该版本 API 文档/浏览器请求确认后调整。
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0, retries: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = max(1, min(retries, 3))

    def list_existing_refs(self) -> set[str]:
        data = self._request("GET", "/api/adventures/")
        return set(_iter_refs(data))

    def create_adventure(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/adventures/", json=payload)

    def attach_immich_asset(self, adventure_id: int | str, asset_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/adventures/{adventure_id}/immich-assets/",
            json={"asset_id": asset_id},
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.request(method, url, headers=headers, **kwargs)
                response.raise_for_status()
                if not response.content:
                    return {}
                data = response.json()
                return data if isinstance(data, dict) else {"items": data}
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt == self.retries - 1:
                    break
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"AdventureLog API request failed: {method} {url}: {last_error}") from last_error


def _iter_refs(data: Any) -> Iterator[str]:
    if isinstance(data, dict):
        direct = data.get("external_ref")
        if isinstance(direct, str) and direct.startswith("aliecs-memory:"):
            yield direct
        description = data.get("description")
        if isinstance(description, str):
            yield from REF_RE.findall(description)
        for value in data.values():
            yield from _iter_refs(value)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_refs(item)
