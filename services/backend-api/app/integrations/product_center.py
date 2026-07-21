"""资料任务外部归档：上传 Paperless-ngx + 在 ERPNext 建档关联。

设计要点：
- 只用标准库 urllib，不引入 HTTP 依赖（与本仓其余集成一致）。
- 本模块不碰数据库：`archive_materials()` 接收当前任务与附件行，返回结构化结果，
  由调用方（store/coordinator）落库，便于 mock 外部 HTTP 单测。
- 幂等：已带 paperless_document_id 的附件跳过重传；ERPNext 记录按任务里已存的
  docname 决定 PUT（更新）还是 POST（新建），失败重试不会重复建记录。
- 网络位置：生产从 aliecs 走现有反向隧道 host.docker.internal:18201(Paperless)/
  :18200(ERPNext)；人类可点链接用 Tailscale/公网地址（见 *_PUBLIC_URL）。
"""

from __future__ import annotations

import json
import os
import time
import uuid as uuid_lib

from dataclasses import dataclass, field
from typing import Any, Callable
from urllib import error, parse, request


DEFAULT_DOCTYPE = "Project"
# ERPNext 资料状态自定义字段（configure-erpnext.sh 权威定义）。
MATERIAL_STATUS_UPLOADED = "识别待确认"


class ProductCenterError(RuntimeError):
    pass


def _clean(value: str) -> str:
    return str(value or "").strip()


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class ProductCenterConfig:
    paperless_api_base: str = ""
    paperless_public_base: str = ""
    paperless_token: str = ""
    paperless_username: str = ""
    paperless_password: str = ""
    erpnext_api_base: str = ""
    erpnext_public_base: str = ""
    erpnext_api_key: str = ""
    erpnext_api_secret: str = ""
    erpnext_doctype: str = DEFAULT_DOCTYPE
    timeout_seconds: float = 30.0
    poll_attempts: int = 30
    poll_interval_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "ProductCenterConfig":
        return cls(
            paperless_api_base=_clean(os.getenv("PRODUCT_CENTER_PAPERLESS_API_BASE", "")),
            paperless_public_base=_clean(
                os.getenv("PRODUCT_CENTER_PAPERLESS_PUBLIC_BASE", "")
            ),
            paperless_token=_clean(os.getenv("PRODUCT_CENTER_PAPERLESS_TOKEN", "")),
            paperless_username=_clean(os.getenv("PRODUCT_CENTER_PAPERLESS_USERNAME", "")),
            paperless_password=_clean(os.getenv("PRODUCT_CENTER_PAPERLESS_PASSWORD", "")),
            erpnext_api_base=_clean(os.getenv("PRODUCT_CENTER_ERPNEXT_API_BASE", "")),
            erpnext_public_base=_clean(os.getenv("PRODUCT_CENTER_ERPNEXT_PUBLIC_BASE", "")),
            erpnext_api_key=_clean(os.getenv("PRODUCT_CENTER_ERPNEXT_API_KEY", "")),
            erpnext_api_secret=_clean(os.getenv("PRODUCT_CENTER_ERPNEXT_API_SECRET", "")),
            erpnext_doctype=_clean(
                os.getenv("PRODUCT_CENTER_ERPNEXT_DOCTYPE", DEFAULT_DOCTYPE)
            )
            or DEFAULT_DOCTYPE,
            timeout_seconds=_positive_float("PRODUCT_CENTER_HTTP_TIMEOUT_SECONDS", 30.0),
            poll_attempts=_positive_int("PRODUCT_CENTER_POLL_ATTEMPTS", 30),
            poll_interval_seconds=_positive_float(
                "PRODUCT_CENTER_POLL_INTERVAL_SECONDS", 2.0
            ),
        )

    @property
    def paperless_ready(self) -> bool:
        has_auth = bool(self.paperless_token) or bool(
            self.paperless_username and self.paperless_password
        )
        return bool(self.paperless_api_base) and has_auth

    @property
    def erpnext_ready(self) -> bool:
        return bool(
            self.erpnext_api_base and self.erpnext_api_key and self.erpnext_api_secret
        )

    @property
    def enabled(self) -> bool:
        """两端都就绪才启用外部归档；否则退化为仅本地归档（行为同以前）。"""
        return self.paperless_ready and self.erpnext_ready


@dataclass
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductCenterError("响应不是合法 JSON") from exc


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 30.0,
) -> HttpResponse:
    req = request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return HttpResponse(int(response.status), response.read())
    except error.HTTPError as exc:
        # HTTPError 也是有响应体的（可含错误详情），保留状态码交给上层判断。
        return HttpResponse(int(exc.code), exc.read() if hasattr(exc, "read") else b"")
    except (error.URLError, TimeoutError, OSError) as exc:
        raise ProductCenterError(f"{method} {url} 连接失败或超时: {exc}") from exc


def _multipart(fields: dict[str, str], filename: str, mime_type: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----aliecs" + uuid_lib.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        parts.append(str(value).encode("utf-8") + b"\r\n")
    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode(
            "utf-8"
        )
    )
    parts.append(f"Content-Type: {mime_type or 'application/octet-stream'}\r\n\r\n".encode("utf-8"))
    parts.append(data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class PaperlessClient:
    def __init__(self, config: ProductCenterConfig, *, sleep: Callable[[float], None] = time.sleep):
        self._config = config
        self._sleep = sleep
        self._token = config.paperless_token

    def _auth_header(self) -> dict[str, str]:
        if not self._token:
            self._token = self._fetch_token()
        return {"Authorization": f"Token {self._token}"}

    def _fetch_token(self) -> str:
        body = parse.urlencode(
            {
                "username": self._config.paperless_username,
                "password": self._config.paperless_password,
            }
        ).encode("utf-8")
        resp = _request(
            "POST",
            self._config.paperless_api_base.rstrip("/") + "/api/token/",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
            timeout=self._config.timeout_seconds,
        )
        if resp.status != 200:
            raise ProductCenterError(f"Paperless 取 token HTTP {resp.status}")
        token = str((resp.json() or {}).get("token") or "")
        if not token:
            raise ProductCenterError("Paperless token 响应为空")
        return token

    def upload(self, data: bytes, filename: str, title: str, mime_type: str) -> str:
        payload, content_type = _multipart({"title": title[:128]}, filename, mime_type, data)
        headers = self._auth_header()
        headers["Content-Type"] = content_type
        resp = _request(
            "POST",
            self._config.paperless_api_base.rstrip("/") + "/api/documents/post_document/",
            headers=headers,
            data=payload,
            timeout=self._config.timeout_seconds,
        )
        if resp.status not in (200, 201):
            raise ProductCenterError(f"Paperless 上传 HTTP {resp.status}")
        # 返回体是被引号包裹的 celery 任务 uuid 字符串。
        task_uuid = str(resp.json() or "").strip().strip('"')
        if not task_uuid:
            raise ProductCenterError("Paperless 上传未返回任务 uuid")
        return task_uuid

    def poll_document_id(self, task_uuid: str) -> int:
        url = (
            self._config.paperless_api_base.rstrip("/")
            + "/api/tasks/?task_id="
            + parse.quote(task_uuid)
        )
        headers = self._auth_header()
        for attempt in range(self._config.poll_attempts):
            resp = _request("GET", url, headers=headers, timeout=self._config.timeout_seconds)
            if resp.status == 200:
                tasks = resp.json() or []
                if isinstance(tasks, dict):
                    tasks = tasks.get("results") or []
                for task in tasks:
                    status = str(task.get("status") or "").upper()
                    if status == "SUCCESS":
                        doc_id = task.get("related_document")
                        if doc_id:
                            return int(doc_id)
                        raise ProductCenterError("Paperless 任务成功但无 related_document（可能重复文档被拒）")
                    if status == "FAILURE":
                        raise ProductCenterError(
                            "Paperless 处理失败: " + str(task.get("result") or "")[:200]
                        )
            if attempt < self._config.poll_attempts - 1:
                self._sleep(self._config.poll_interval_seconds)
        raise ProductCenterError("Paperless 轮询超时，文档未就绪")

    def document_url(self, doc_id: int) -> str:
        base = (self._config.paperless_public_base or self._config.paperless_api_base).rstrip("/")
        return f"{base}/documents/{doc_id}/details"


class ErpNextClient:
    def __init__(self, config: ProductCenterConfig):
        self._config = config

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self._config.erpnext_api_key}:{self._config.erpnext_api_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _resource_url(self, doctype: str, name: str = "") -> str:
        base = self._config.erpnext_api_base.rstrip("/") + "/api/resource/" + parse.quote(doctype)
        return base + "/" + parse.quote(name) if name else base

    def create(self, doctype: str, payload: dict[str, Any]) -> str:
        resp = _request(
            "POST",
            self._resource_url(doctype),
            headers=self._headers(),
            data=json.dumps(payload).encode("utf-8"),
            timeout=self._config.timeout_seconds,
        )
        if resp.status not in (200, 201):
            raise ProductCenterError(f"ERPNext 创建 {doctype} HTTP {resp.status}: {resp.body[:300]!r}")
        name = str(((resp.json() or {}).get("data") or {}).get("name") or "")
        if not name:
            raise ProductCenterError("ERPNext 创建响应无 name")
        return name

    def update(self, doctype: str, name: str, payload: dict[str, Any]) -> str:
        resp = _request(
            "PUT",
            self._resource_url(doctype, name),
            headers=self._headers(),
            data=json.dumps(payload).encode("utf-8"),
            timeout=self._config.timeout_seconds,
        )
        if resp.status not in (200, 201):
            raise ProductCenterError(f"ERPNext 更新 {doctype}/{name} HTTP {resp.status}: {resp.body[:300]!r}")
        return name

    def record_url(self, doctype: str, name: str) -> str:
        base = (self._config.erpnext_public_base or self._config.erpnext_api_base).rstrip("/")
        slug = doctype.lower().replace(" ", "-")
        return f"{base}/app/{slug}/{parse.quote(name)}"


@dataclass
class ItemArchiveResult:
    item_id: int
    document_id: int | None = None
    document_url: str = ""
    task_uuid: str = ""
    error: str = ""


@dataclass
class ArchiveResult:
    status: str = "none"  # completed / partial / failed
    items: list[ItemArchiveResult] = field(default_factory=list)
    erpnext_doctype: str = ""
    erpnext_docname: str = ""
    erpnext_url: str = ""
    error: str = ""

    @property
    def document_count(self) -> int:
        return sum(1 for item in self.items if item.document_id)


def _project_payload(
    task: dict[str, Any], doc_ids: list[int], doc_urls: list[str], is_create: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "custom_paperless_document_ids": "\n".join(str(i) for i in doc_ids),
        "custom_paperless_document_urls": "\n".join(doc_urls),
        "custom_material_data_status": MATERIAL_STATUS_UPLOADED,
    }
    if is_create:
        title = str(task.get("title") or "微信资料任务").strip()[:140]
        # project_name 追加短 task_key，保证可读且不易撞名。
        payload["project_name"] = f"{title} #{str(task.get('task_key') or '')[:8]}"
    return payload


def archive_materials(
    config: ProductCenterConfig,
    task: dict[str, Any],
    items: list[dict[str, Any]],
    read_original: Callable[[dict[str, Any]], bytes | None],
    *,
    paperless: PaperlessClient | None = None,
    erpnext: ErpNextClient | None = None,
) -> ArchiveResult:
    """把任务的原件上传 Paperless 并在 ERPNext 建/更新记录。返回结果，不落库。"""
    paperless = paperless or PaperlessClient(config)
    erpnext = erpnext or ErpNextClient(config)
    result = ArchiveResult()

    media_items = [it for it in items if str(it.get("storage_path") or "")]
    all_doc_ids: list[int] = []
    all_doc_urls: list[str] = []
    failures = 0

    for item in media_items:
        item_id = int(item["id"])
        existing_id = item.get("paperless_document_id")
        if existing_id:
            # 幂等：已上传过，复用。
            all_doc_ids.append(int(existing_id))
            url = str(item.get("paperless_document_url") or "") or paperless.document_url(int(existing_id))
            all_doc_urls.append(url)
            result.items.append(
                ItemArchiveResult(item_id, int(existing_id), url, str(item.get("paperless_task_uuid") or ""))
            )
            continue
        try:
            data = read_original(item)
            if not data:
                raise ProductCenterError("原件读取为空")
            title = str(item.get("original_filename") or f"material-{item_id}")
            task_uuid = paperless.upload(data, title, title, str(item.get("mime_type") or ""))
            doc_id = paperless.poll_document_id(task_uuid)
            url = paperless.document_url(doc_id)
            all_doc_ids.append(doc_id)
            all_doc_urls.append(url)
            result.items.append(ItemArchiveResult(item_id, doc_id, url, task_uuid))
        except ProductCenterError as exc:
            failures += 1
            result.items.append(ItemArchiveResult(item_id, error=str(exc)[:400]))

    # 即便部分附件失败，也把已成功的写进 ERPNext（可观测、可重试补齐）。
    doctype = config.erpnext_doctype or DEFAULT_DOCTYPE
    try:
        existing_name = str(task.get("erpnext_docname") or "").strip()
        if existing_name:
            payload = _project_payload(task, all_doc_ids, all_doc_urls, is_create=False)
            name = erpnext.update(doctype, existing_name, payload)
        else:
            payload = _project_payload(task, all_doc_ids, all_doc_urls, is_create=True)
            name = erpnext.create(doctype, payload)
        result.erpnext_doctype = doctype
        result.erpnext_docname = name
        result.erpnext_url = erpnext.record_url(doctype, name)
    except ProductCenterError as exc:
        result.status = "failed"
        result.error = str(exc)[:400]
        return result

    result.status = "completed" if failures == 0 else "partial"
    if failures:
        result.error = f"{failures} 个附件上传失败"
    return result
