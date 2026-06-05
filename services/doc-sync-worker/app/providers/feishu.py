from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests
from requests import RequestException


BASE = "https://open.feishu.cn/open-apis"
ENV_LIST_SEPARATORS = (";", ",", "\n")
MAX_NUMBERED_ENV_ITEMS = 20
NETWORK_RETRIES = 3
RETRY_SLEEP_SECONDS = 1.5
MAX_RECORD_PAGES = 1000


@dataclass(frozen=True)
class FeishuCredential:
    env_profile: str
    app_id: str
    app_secret: str
    api_base: str = BASE


@dataclass(frozen=True)
class FeishuBitableSource:
    env_profile: str
    app_token: str
    table_id: str
    source_name: str
    view_id: str = ""
    wiki_node_token: str = ""
    source_url: str = ""


def normalize_env_profile(profile: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (profile or "").strip()).strip("_").upper()


def split_env_list(value: str) -> list[str]:
    text = str(value or "")
    for sep in ENV_LIST_SEPARATORS[1:]:
        text = text.replace(sep, ENV_LIST_SEPARATORS[0])
    return [item.strip() for item in text.split(ENV_LIST_SEPARATORS[0]) if item.strip()]


def profiled_env_candidates(key: str, namespace: str | None = None, profile: str | None = None) -> list[str]:
    normalized_profile = normalize_env_profile(profile)
    normalized_namespace = normalize_env_profile(namespace)
    normalized_key = normalize_env_profile(key)
    candidates: list[str] = []

    if normalized_profile:
        if normalized_namespace and normalized_key.startswith(f"{normalized_namespace}_"):
            rest = normalized_key[len(normalized_namespace) + 1 :]
            candidates.append(f"{normalized_namespace}_{normalized_profile}_{rest}")
        elif normalized_namespace:
            candidates.append(f"{normalized_namespace}_{normalized_profile}_{normalized_key}")

        if "_" in normalized_key:
            first, rest = normalized_key.split("_", 1)
            candidates.append(f"{first}_{normalized_profile}_{rest}")

        candidates.extend([f"{normalized_key}_{normalized_profile}", f"{normalized_profile}_{normalized_key}"])

    candidates.append(normalized_key)
    return list(dict.fromkeys(candidates))


def get_profiled_env(
    key: str,
    namespace: str | None,
    profile: str,
    default: str = "",
    fallback_legacy: bool = False,
) -> str:
    candidates = profiled_env_candidates(key, namespace=namespace, profile=profile)
    if profile and not fallback_legacy:
        candidates = candidates[:-1]
    for candidate in candidates:
        value = os.getenv(candidate)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def profiled_env_values(
    key: str,
    namespace: str | None,
    profile: str,
    fallback_legacy: bool = False,
) -> list[str]:
    values: list[str] = []
    candidates = profiled_env_candidates(key, namespace=namespace, profile=profile)
    if profile and not fallback_legacy:
        candidates = candidates[:-1]

    for candidate in candidates:
        env_keys = [candidate, f"{candidate}S"]
        for index in range(1, MAX_NUMBERED_ENV_ITEMS + 1):
            env_keys.extend([f"{candidate}_{index}", f"{candidate}{index}"])
        for env_key in env_keys:
            values.extend(split_env_list(os.getenv(env_key, "")))

    return list(dict.fromkeys(values))


def env_profiles(profiles_arg: str = "") -> list[str]:
    profiles = split_env_list(profiles_arg or os.getenv("FEISHU_ENV_PROFILES", "") or os.getenv("FEISHU_ENV_PROFILE", ""))
    normalized = [normalize_env_profile(item) for item in profiles if normalize_env_profile(item)]
    if normalized:
        return normalized

    inferred: set[str] = set()
    for key in os.environ:
        match = re.match(r"^FEISHU_(.+)_(?:APP_ID|APP_SECRET|APP_TOKEN|TABLE_ID)$", key)
        if match:
            inferred.add(normalize_env_profile(match.group(1)))
    return sorted(item for item in inferred if item)


def credentials_for_profile(profile: str) -> list[FeishuCredential]:
    normalized = normalize_env_profile(profile)
    app_id = get_profiled_env("APP_ID", "FEISHU", normalized)
    app_secret = get_profiled_env("APP_SECRET", "FEISHU", normalized)
    api_base = get_profiled_env("API_BASE", "FEISHU", normalized, default=BASE, fallback_legacy=True)
    if not app_id or not app_secret:
        return []
    return [FeishuCredential(env_profile=normalized, app_id=app_id, app_secret=app_secret, api_base=api_base)]


def discover_profile_sources(profile: str) -> list[FeishuBitableSource]:
    normalized = normalize_env_profile(profile)
    app_tokens = profiled_env_values("APP_TOKEN", "FEISHU", normalized)
    table_ids = profiled_env_values("TABLE_ID", "FEISHU", normalized)
    view_ids = profiled_env_values("VIEW_ID", "FEISHU", normalized)
    wiki_nodes = profiled_env_values("WIKI_NODE_TOKEN", "FEISHU", normalized)
    wiki_urls = profiled_env_values("WIKI_URL", "FEISHU", normalized)
    source_name = (
        get_profiled_env("TABLE_NAME", "FEISHU", normalized)
        or get_profiled_env("APP_NAME", "FEISHU", normalized)
        or f"{normalized} 飞书多维表格"
    )

    sources: list[FeishuBitableSource] = []
    max_count = max(len(app_tokens), len(table_ids), len(wiki_nodes), len(wiki_urls), 1)
    for index in range(max_count):
        app_token = app_tokens[index] if index < len(app_tokens) else ""
        table_id = table_ids[index] if index < len(table_ids) else (table_ids[0] if table_ids else "")
        wiki_node = wiki_nodes[index] if index < len(wiki_nodes) else ""
        source_url = wiki_urls[index] if index < len(wiki_urls) else ""
        view_id = view_ids[index] if index < len(view_ids) else ""
        if not table_id or not (app_token or wiki_node):
            continue
        sources.append(
            FeishuBitableSource(
                env_profile=normalized,
                app_token=app_token,
                table_id=table_id,
                view_id=view_id,
                wiki_node_token=wiki_node,
                source_url=source_url,
                source_name=source_name if max_count == 1 else f"{source_name} {index + 1}",
            )
        )
    return sources


class FeishuBitableClient:
    def __init__(self, app_id: str, app_secret: str, api_base: str = BASE, timeout: int = 20) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._tenant_token: str | None = None
        self.session = requests.Session()
        self.session.trust_env = False

    def _redact_text(self, value: Any) -> str:
        text = str(value or "")
        for secret in (self.app_secret, self._tenant_token):
            if secret:
                text = text.replace(str(secret), "***")
        return text

    @staticmethod
    def _redact_path(path: str) -> str:
        return re.sub(r"(/bitable/v1/apps/)[^/]+", r"\1***", str(path or ""))

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        last_http_status: int | None = None
        url = f"{self.api_base}{path}"
        safe_path = self._redact_path(path)
        for attempt in range(1, NETWORK_RETRIES + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") not in (0, None):
                    raise RuntimeError(
                        f"{safe_path} failed: code={data.get('code')} msg={self._redact_text(data.get('msg'))}"
                    )
                return data
            except RequestException as exc:
                last_error = exc
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)
                if status_code is not None:
                    last_http_status = int(status_code)
                if attempt < NETWORK_RETRIES:
                    time.sleep(RETRY_SLEEP_SECONDS * attempt)
                    continue
            except ValueError as exc:
                raise RuntimeError(f"飞书接口返回的不是 JSON：{safe_path}") from exc

        http_part = f" http_status={last_http_status}" if last_http_status is not None else ""
        raise RuntimeError(
            "飞书接口连接失败，可能是网络、代理或远端临时断开。"
            f" 请求路径：{safe_path}"
            f"{http_part}"
            f" 原始错误类型：{type(last_error).__name__ if last_error else 'unknown'}"
        ) from last_error

    def tenant_token(self) -> str:
        if self._tenant_token:
            return self._tenant_token
        data = self._request_json(
            "POST",
            "/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError("tenant_access_token response missing token")
        self._tenant_token = str(token)
        return self._tenant_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tenant_token()}", "Content-Type": "application/json"}

    def resolve_app_token_from_wiki_node(self, wiki_node_token: str) -> str:
        data = self._request_json(
            "GET",
            "/wiki/v2/spaces/get_node",
            headers=self._headers(),
            params={"token": wiki_node_token},
        )
        node = (data.get("data") or {}).get("node") or (data.get("data") or {})
        obj_type = node.get("obj_type")
        obj_token = node.get("obj_token")
        if obj_type not in {"bitable", "docx", "sheet"} or not obj_token:
            raise RuntimeError(f"wiki node is not a usable bitable node: obj_type={obj_type}")
        return str(obj_token)

    def list_fields(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        data = self._request_json(
            "GET",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            headers=self._headers(),
        )
        return (data.get("data") or {}).get("items") or []

    def get_records(
        self,
        app_token: str,
        table_id: str,
        view_id: str = "",
        page_size: int = 500,
        max_pages: int = MAX_RECORD_PAGES,
    ) -> dict[str, Any]:
        page_token = ""
        seen_page_tokens: set[str] = set()
        records: list[dict[str, Any]] = []
        page_count = 0
        while page_count < max_pages:
            params = {"page_size": min(max(page_size, 1), 500)}
            if page_token:
                params["page_token"] = page_token
            if view_id:
                params["view_id"] = view_id
            data = self._request_json(
                "GET",
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                headers=self._headers(),
                params=params,
            )
            block = data.get("data") or {}
            records.extend(block.get("items") or [])
            page_count += 1
            if not block.get("has_more"):
                break
            next_page_token = str(block.get("page_token") or "")
            if not next_page_token:
                raise RuntimeError("飞书记录分页异常：has_more=true 但缺少 page_token。")
            if next_page_token in seen_page_tokens:
                raise RuntimeError("飞书记录分页异常：page_token 重复，已停止同步。")
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token
        else:
            raise RuntimeError(f"飞书记录分页超过最大页数：{max_pages}。")
        return {"records": records, "fetched_count": len(records), "page_count": page_count}
