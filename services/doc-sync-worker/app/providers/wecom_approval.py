from __future__ import annotations

from typing import Any

import requests
from requests import RequestException

from app.providers.wecom import BASE, NETWORK_RETRIES, RETRY_SLEEP_SECONDS, WeComSmartsheetClient


class WeComApprovalClient(WeComSmartsheetClient):
    def get_approval_detail(self, sp_no: str) -> dict[str, Any]:
        return self._post("/oa/getapprovaldetail", {"sp_no": sp_no})

    def download_media(self, media_id: str) -> bytes:
        url = f"{BASE}/media/get"
        return self._download_binary(url, params={"access_token": self.access_token(), "media_id": media_id})

    def download_url(self, url: str) -> bytes:
        return self._download_binary(url)

    def _download_binary(self, url: str, **kwargs: Any) -> bytes:
        last_error: Exception | None = None
        for attempt in range(1, NETWORK_RETRIES + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                return resp.content
            except RequestException as exc:
                last_error = exc
                if attempt < NETWORK_RETRIES:
                    import time

                    time.sleep(RETRY_SLEEP_SECONDS * attempt)
                    continue
        raise RuntimeError(
            "企业微信媒体下载失败，可能是权限、网络或文件已过期。"
            f" 请求地址：{self._redact_url(url)}"
            f" 原始错误类型：{type(last_error).__name__ if last_error else 'unknown'}"
        )
