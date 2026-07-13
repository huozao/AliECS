from __future__ import annotations

import json

from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - runtime dependency guard
    requests = None

from config.settings import Settings, load_settings
from tplus_datahub.chanjet.auth import build_auth_headers
from tplus_datahub.core.exceptions import ChanjetAPIError
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import text_preview


_MESSAGE_KEYS = ("message", "Message", "msg", "Msg", "error", "Error", "detail", "Detail")


def _business_message(text: str) -> str:
    """从 T+ 错误返回体提取业务错误原文；解析不了返回空串。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return ""

    def walk(node: Any) -> str:
        if isinstance(node, dict):
            for key in _MESSAGE_KEYS:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in node.values():
                if isinstance(value, (dict, list)):
                    nested = walk(value)
                    if nested:
                        return nested
        elif isinstance(node, list):
            for value in node:
                nested = walk(value)
                if nested:
                    return nested
        return ""

    return walk(data)


class ChanjetClient:
    def __init__(self, settings: Settings | None = None, session: Any | None = None):
        self.settings = settings or load_settings()
        if session is None:
            if requests is None:
                raise ImportError("缺少 requests，请先运行 pip install -r requirements.txt")
            session = requests.Session()
        self.session = session
        self.logger = get_logger("tplus_datahub.chanjet")

    def post(self, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        url = self._url(endpoint)
        body = dict(payload or {})
        headers = build_auth_headers(self.settings)

        self.logger.info("请求接口：%s", endpoint)
        try:
            response = self.session.post(url, json=body, headers=headers, timeout=self.settings.timeout)
        except Exception as exc:  # requests exceptions vary by adapter/session
            raise ChanjetAPIError(f"请求失败：{exc}", endpoint=endpoint) from exc

        status_code = getattr(response, "status_code", None)
        self.logger.info("接口状态码：%s", status_code)
        body_preview = text_preview(getattr(response, "text", ""), 300)

        if status_code is not None and status_code >= 400:
            raw_text = getattr(response, "text", "")
            business = _business_message(raw_text)
            raise ChanjetAPIError(
                message=f"T+ 返回错误：{business}" if business else f"接口返回 HTTP {status_code}",
                endpoint=endpoint,
                status_code=status_code,
                body_preview=text_preview(raw_text, 1000),
                business_message=business,
            )

        try:
            result = response.json()
        except Exception as exc:
            raise ChanjetAPIError(
                message="接口返回内容不是合法 JSON",
                endpoint=endpoint,
                status_code=status_code,
                body_preview=body_preview,
            ) from exc

        # T+ Create 类接口成功时可能返回裸标量（如新记录 ID）或 null（Query 无结果），
        # 一律原样返回，由调用方按语义处理（BOM 写入以写后查询验证为权威）。
        return result

    def _url(self, endpoint: str) -> str:
        path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        return f"{self.settings.base_url}{path}"
