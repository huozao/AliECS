"""Minimal Aliyun OSS V1-signature client.

Stdlib only (urllib + hmac + hashlib + base64), matching the style of
`immich_client.py` and `_webdock_photo_request`. Implements just enough of
the OSS REST API for photo storage: PUT object, DELETE object, and building
public object URLs for the virtual-hosted-style bucket domain.

Reference: https://help.aliyun.com/document_detail/31951.html (V1 signature)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import formatdate


@dataclass
class OssConfig:
    endpoint: str
    bucket: str
    access_key_id: str
    access_key_secret: str
    timeout_seconds: float = 30.0

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key_id and self.access_key_secret)


def config_from_env() -> OssConfig:
    return OssConfig(
        endpoint=os.getenv("OSS_ENDPOINT", "").strip(),
        bucket=os.getenv("OSS_BUCKET", "").strip(),
        access_key_id=os.getenv("OSS_ACCESS_KEY_ID", "").strip(),
        access_key_secret=os.getenv("OSS_ACCESS_KEY_SECRET", "").strip(),
        timeout_seconds=float(os.getenv("OSS_TIMEOUT_SECONDS", "30")),
    )


class OssError(Exception):
    pass


class OssClient:
    def __init__(self, config: OssConfig) -> None:
        self.config = config

    def object_url(self, key: str) -> str:
        return f"https://{self.config.bucket}.{self.config.endpoint}/{key}"

    def _signed_headers(self, method: str, key: str, *, content_type: str = "") -> dict[str, str]:
        date = formatdate(usegmt=True)
        resource = f"/{self.config.bucket}/{key}"
        string_to_sign = f"{method}\n\n{content_type}\n{date}\n{resource}"
        signature = base64.b64encode(
            hmac.new(
                self.config.access_key_secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("utf-8")
        headers = {
            "Date": date,
            "Authorization": f"OSS {self.config.access_key_id}:{signature}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def put_object(self, key: str, content: bytes, content_type: str) -> None:
        headers = self._signed_headers("PUT", key, content_type=content_type)
        request = urllib.request.Request(
            self.object_url(key), data=content, headers=headers, method="PUT"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise OssError(f"OSS PUT {key} failed: HTTP {exc.code}: {body[:300]}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise OssError(f"OSS PUT {key} failed: {exc}") from exc

    def delete_object(self, key: str) -> None:
        headers = self._signed_headers("DELETE", key)
        request = urllib.request.Request(self.object_url(key), headers=headers, method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return
            body = exc.read().decode("utf-8", errors="replace")
            raise OssError(f"OSS DELETE {key} failed: HTTP {exc.code}: {body[:300]}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise OssError(f"OSS DELETE {key} failed: {exc}") from exc
