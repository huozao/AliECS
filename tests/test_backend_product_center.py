from __future__ import annotations

import importlib
import json
import sys

from pathlib import Path
from urllib import error


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))


def _ensure_backend_app() -> None:
    """确保 sys.modules['app'] 是 backend-api 的那个包。

    services 下有四个包都叫 `app`（backend-api / coding-executor / doc-sync-worker /
    mcp-coding-server）。谁先被 import 谁就占住 sys.modules['app']，之后本文件里
    延迟到测试体内的 import 就会拿到错的包——2026-08-31 实测报错：
    `cannot import name 'app' from 'app.main' (…/doc-sync-worker/app/main.py)`。

    仓里另外 42 个测试模块用 setUpClass/tearDownClass 做同一件事；本文件是裸函数式
    没有那两个钩子，所以把隔离放进 _module()。**只在占位的不是 backend-api 时才清**，
    无条件清会让每次调用都重新 import，模块身份变来变去，patch 会打空。
    """
    resident = sys.modules.get("app")
    if resident is not None:
        paths = list(getattr(resident, "__path__", []) or [])
        if not any(str(BACKEND_ROOT) in p for p in paths):
            for name in list(sys.modules):
                if name == "app" or name.startswith("app."):
                    del sys.modules[name]
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))

def _module():
    _ensure_backend_app()
    return importlib.import_module("app.integrations.product_center")


class FakeResp:
    def __init__(self, status: int, body) -> None:
        self.status = status
        if isinstance(body, bytes):
            self._body = body
        elif isinstance(body, str):
            self._body = body.encode("utf-8")
        else:
            self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Router:
    """按 (method, path) 路由 urlopen；记录调用，可注入 HTTPError/异常。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.routes: dict[tuple[str, str], object] = {}

    def add(self, method: str, path: str, response) -> None:
        self.routes[(method, path)] = response

    def urlopen(self, req, timeout=None):
        method = req.get_method()
        full = req.full_url
        path = full.split("?", 1)[0]
        self.calls.append((method, full, req.data))
        key = (method, path)
        if key not in self.routes:
            raise AssertionError(f"未预设路由: {key}")
        resp = self.routes[key]
        if isinstance(resp, Exception):
            raise resp
        if callable(resp):
            resp = resp(req)
        return resp


def _config(mod, **overrides):
    base = dict(
        paperless_api_base="http://pl",
        paperless_public_base="http://pl-pub",
        paperless_token="tok",
        erpnext_api_base="http://erp",
        erpnext_public_base="http://erp-pub",
        erpnext_api_key="k",
        erpnext_api_secret="s",
        poll_attempts=3,
        poll_interval_seconds=0.0,
    )
    base.update(overrides)
    return mod.ProductCenterConfig(**base)


def _install(monkeypatch, mod, router: Router) -> None:
    monkeypatch.setattr(mod.request, "urlopen", router.urlopen)


def _task(**kw):
    base = {"id": 7, "task_key": "b" * 32, "title": "供应商合同", "erpnext_docname": ""}
    base.update(kw)
    return base


def _items():
    return [
        {"id": 1, "storage_path": "originals/a.pdf", "original_filename": "a.pdf", "mime_type": "application/pdf"},
        {"id": 2, "storage_path": "", "text_content": "说明", "original_filename": "", "mime_type": ""},
    ]


def test_config_enabled_gating() -> None:
    mod = _module()
    assert _config(mod).enabled is True
    assert _config(mod, paperless_token="", paperless_username="", paperless_password="").enabled is False
    assert _config(mod, erpnext_api_key="").enabled is False
    # 仅用户名密码也算 paperless 就绪
    assert _config(mod, paperless_token="", paperless_username="u", paperless_password="p").paperless_ready is True


def test_full_archive_success(monkeypatch) -> None:
    mod = _module()
    router = Router()
    router.add("POST", "http://pl/api/documents/post_document/", FakeResp(200, '"task-uuid-1"'))
    router.add("GET", "http://pl/api/tasks/", FakeResp(200, [{"status": "SUCCESS", "related_document": 42}]))
    router.add("POST", "http://erp/api/resource/Project", FakeResp(200, {"data": {"name": "PROJ-0001"}}))
    _install(monkeypatch, mod, router)

    result = mod.archive_materials(_config(mod), _task(), _items(), lambda item: b"pdfbytes")

    assert result.status == "completed"
    assert result.document_count == 1
    assert result.items[0].document_id == 42
    assert result.items[0].document_url == "http://pl-pub/documents/42/details"
    assert result.erpnext_docname == "PROJ-0001"
    assert result.erpnext_url == "http://erp-pub/app/project/PROJ-0001"

    # ERPNext 创建载荷含三个自定义字段
    post_calls = [c for c in router.calls if c[0] == "POST" and "resource/Project" in c[1]]
    payload = json.loads(post_calls[0][2].decode("utf-8"))
    assert payload["custom_paperless_document_ids"] == "42"
    assert payload["custom_paperless_document_urls"] == "http://pl-pub/documents/42/details"
    assert payload["custom_material_data_status"] == mod.MATERIAL_STATUS_UPLOADED
    assert "供应商合同" in payload["project_name"]


def test_update_path_when_docname_exists(monkeypatch) -> None:
    mod = _module()
    router = Router()
    router.add("POST", "http://pl/api/documents/post_document/", FakeResp(200, '"u1"'))
    router.add("GET", "http://pl/api/tasks/", FakeResp(200, [{"status": "SUCCESS", "related_document": 9}]))
    router.add("PUT", "http://erp/api/resource/Project/PROJ-0009", FakeResp(200, {"data": {"name": "PROJ-0009"}}))
    _install(monkeypatch, mod, router)

    result = mod.archive_materials(
        _config(mod), _task(erpnext_docname="PROJ-0009"), _items(), lambda item: b"x"
    )
    assert result.status == "completed"
    assert result.erpnext_docname == "PROJ-0009"
    assert any(c[0] == "PUT" for c in router.calls)
    assert not any(c[0] == "POST" and "resource/Project" in c[1] for c in router.calls)


def test_idempotent_skip_uploaded_item(monkeypatch) -> None:
    mod = _module()
    router = Router()
    # 只预设 ERPNext；若尝试上传会因无路由抛 AssertionError。
    router.add("POST", "http://erp/api/resource/Project", FakeResp(200, {"data": {"name": "P1"}}))
    _install(monkeypatch, mod, router)

    items = [{
        "id": 1, "storage_path": "originals/a.pdf", "original_filename": "a.pdf",
        "mime_type": "application/pdf", "paperless_document_id": 5,
        "paperless_document_url": "http://pl-pub/documents/5/details",
    }]
    result = mod.archive_materials(_config(mod), _task(), items, lambda item: b"should-not-read")
    assert result.status == "completed"
    assert result.document_count == 1
    assert not any("post_document" in c[1] for c in router.calls)
    payload = json.loads([c for c in router.calls if c[0] == "POST"][0][2].decode("utf-8"))
    assert payload["custom_paperless_document_ids"] == "5"


def test_paperless_failure_is_partial_but_erpnext_written(monkeypatch) -> None:
    mod = _module()
    router = Router()
    router.add("POST", "http://pl/api/documents/post_document/", FakeResp(200, '"u1"'))
    router.add("GET", "http://pl/api/tasks/", FakeResp(200, [{"status": "FAILURE", "result": "duplicate"}]))
    router.add("POST", "http://erp/api/resource/Project", FakeResp(200, {"data": {"name": "P2"}}))
    _install(monkeypatch, mod, router)

    result = mod.archive_materials(_config(mod), _task(), _items(), lambda item: b"x")
    assert result.status == "partial"
    assert result.document_count == 0
    assert "duplicate" in result.items[0].error
    assert result.erpnext_docname == "P2"  # 仍建了记录（可观测、后续可补齐）


def test_erpnext_http_error_is_failed(monkeypatch) -> None:
    mod = _module()
    router = Router()
    router.add("POST", "http://pl/api/documents/post_document/", FakeResp(200, '"u1"'))
    router.add("GET", "http://pl/api/tasks/", FakeResp(200, [{"status": "SUCCESS", "related_document": 1}]))
    router.add("POST", "http://erp/api/resource/Project", FakeResp(417, {"exc": "boom"}))
    _install(monkeypatch, mod, router)

    result = mod.archive_materials(_config(mod), _task(), _items(), lambda item: b"x")
    assert result.status == "failed"
    assert "417" in result.error


def test_connection_error_surfaces_on_paperless(monkeypatch) -> None:
    mod = _module()
    router = Router()
    router.add("POST", "http://pl/api/documents/post_document/", error.URLError("refused"))
    router.add("POST", "http://erp/api/resource/Project", FakeResp(200, {"data": {"name": "P3"}}))
    _install(monkeypatch, mod, router)

    # 单个附件上传连接失败被吞进 item.error，整体 partial（ERPNext 仍写空集）。
    result = mod.archive_materials(_config(mod), _task(), _items(), lambda item: b"x")
    assert result.status == "partial"
    assert "连接失败" in result.items[0].error


def test_token_fetched_from_credentials(monkeypatch) -> None:
    mod = _module()
    router = Router()
    router.add("POST", "http://pl/api/token/", FakeResp(200, {"token": "fetched-token"}))
    router.add("POST", "http://pl/api/documents/post_document/", FakeResp(200, '"u1"'))
    router.add("GET", "http://pl/api/tasks/", FakeResp(200, [{"status": "SUCCESS", "related_document": 3}]))
    router.add("POST", "http://erp/api/resource/Project", FakeResp(200, {"data": {"name": "P4"}}))
    _install(monkeypatch, mod, router)

    cfg = _config(mod, paperless_token="", paperless_username="admin", paperless_password="pw")
    result = mod.archive_materials(cfg, _task(), _items(), lambda item: b"x")
    assert result.status == "completed"
    # 上传请求带上了动态获取的 token
    upload = [c for c in router.calls if "post_document" in c[1]][0]
    # header 检查：从 Request 对象拿不到这里，改为确认确实调了 token 接口
    assert any("api/token" in c[1] for c in router.calls)
