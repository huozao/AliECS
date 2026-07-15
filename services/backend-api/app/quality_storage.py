"""质检报告 WebDAV 文件层：单文件单副本，账号选择与月上传额度由数据库控制。"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import BinaryIO, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StorageBackend:
    id: int
    code: str
    provider: str
    display_name: str
    credential_ref: str
    base_path: str


class WebDavStorage:
    def __init__(self, backend: StorageBackend) -> None:
        if backend.provider != "nutstore_webdav":
            raise StorageError(f"不支持的存储驱动：{backend.provider}")
        prefix = backend.credential_ref
        self.base_url = os.getenv(f"{prefix}_URL", "https://dav.jianguoyun.com/dav/").rstrip("/") + "/"
        user = os.getenv(f"{prefix}_USER", "")
        password = os.getenv(f"{prefix}_PASS", "")
        if not user or not password:
            raise StorageError(f"存储凭据未配置：{prefix}")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth_header = f"Basic {token}"
        self.backend = backend

    def _url(self, remote_path: str) -> str:
        clean = "/".join(part for part in remote_path.replace("\\", "/").split("/") if part)
        return self.base_url + quote(clean, safe="/")

    def _request(self, method: str, remote_path: str, *, data: bytes | None = None, headers: dict[str, str] | None = None):
        request_headers = {"Authorization": self.auth_header, **(headers or {})}
        request = Request(self._url(remote_path), data=data, headers=request_headers, method=method)
        try:
            return urlopen(request, timeout=45)
        except HTTPError as exc:
            raise StorageError(f"WebDAV {method} 失败：HTTP {exc.code}") from exc
        except URLError as exc:
            raise StorageError(f"WebDAV {method} 连接失败") from exc

    def ensure_directory(self, remote_dir: str) -> None:
        current: list[str] = []
        for part in (p for p in remote_dir.split("/") if p):
            current.append(part)
            path = "/".join(current)
            request = Request(self._url(path), headers={"Authorization": self.auth_header}, method="MKCOL")
            try:
                with urlopen(request, timeout=30):
                    pass
            except HTTPError as exc:
                if exc.code not in {405, 301, 302}:
                    raise StorageError(f"WebDAV MKCOL 失败：HTTP {exc.code}") from exc
            except URLError as exc:
                raise StorageError("WebDAV MKCOL 连接失败") from exc

    def put(self, remote_path: str, data: bytes, mime_type: str) -> None:
        parent = remote_path.rsplit("/", 1)[0]
        self.ensure_directory(parent)
        with self._request("PUT", remote_path, data=data, headers={"Content-Type": mime_type}):
            pass

    def delete(self, remote_path: str) -> None:
        try:
            with self._request("DELETE", remote_path):
                pass
        except StorageError:
            return

    def stream(self, remote_path: str, chunk_size: int = 1024 * 1024) -> tuple[BinaryIO, Iterator[bytes]]:
        response = self._request("GET", remote_path)

        def chunks() -> Iterator[bytes]:
            try:
                while chunk := response.read(chunk_size):
                    yield chunk
            finally:
                response.close()

        return response, chunks()

    def health_check(self) -> None:
        self.ensure_directory(self.backend.base_path)
        with self._request("PROPFIND", self.backend.base_path, headers={"Depth": "0"}):
            pass
