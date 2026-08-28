from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from requests import RequestException


BASE = "https://qyapi.weixin.qq.com/cgi-bin"
ENV_LIST_SEPARATORS = (";", ",", "\n")
MAX_NUMBERED_ENV_ITEMS = 20
NETWORK_RETRIES = 3
RETRY_SLEEP_SECONDS = 1.5
RECORD_PAGE_SIZE = 50
MAX_RECORD_PAGES = 2000
# 某条记录的成员字段引用了本企业解析不到的 userid 时，包含它的那次 get_records 整体失败：
# 多条请求回 60111、单条请求回 -1。2026-08-28 在 COMPANY_A「公开的生产记录表」实测，
# 146 条里第 85/86/97 条稳定不可读；用 field_ids 裁掉成员字段也绕不开——
# 企微在返回前先解析 userid，字段过滤发生在解析之后。
UNREADABLE_RECORD_ERRCODES = {-1, 60111}
SENSITIVE_URL_PARAMS = {
    "access_token",
    "authcode",
    "code",
    "corpsecret",
    "scode",
    "sid",
    "skey",
    "ticket",
    "token",
    "wedrive_sid",
    "wedrive_skey",
    "wedrive_ticket",
    "wwmng_authcode",
}


@dataclass(frozen=True)
class WeComCredential:
    env_profile: str
    corpid: str
    secret: str
    label: str


@dataclass(frozen=True)
class WeComDocSource:
    env_profile: str
    docid: str
    source_name: str
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
    profiles = split_env_list(profiles_arg or os.getenv("WECOM_ENV_PROFILES", ""))
    normalized = [normalize_env_profile(item) for item in profiles if normalize_env_profile(item)]
    if normalized:
        return normalized

    inferred: set[str] = set()
    for key in os.environ:
        match = re.match(r"^WECOM_(.+)_(?:CORP_ID|APP_SECRET|APP_SECRET_2|APP_SECRETS)$", key)
        if match:
            inferred.add(normalize_env_profile(match.group(1)))
    return sorted(item for item in inferred if item)


def credentials_for_profile(profile: str) -> list[WeComCredential]:
    normalized = normalize_env_profile(profile)
    corpid = get_profiled_env("CORP_ID", "WECOM", normalized)
    secrets = profiled_env_values("APP_SECRET", "WECOM", normalized)
    extra_secret = get_profiled_env("APP_SECRET_2", "WECOM", normalized)
    if extra_secret:
        secrets.append(extra_secret)
    secrets = list(dict.fromkeys(secret for secret in secrets if secret))

    if not corpid or not secrets:
        return []

    return [
        WeComCredential(env_profile=normalized, corpid=corpid, secret=secret, label=f"{normalized}#{index}")
        for index, secret in enumerate(secrets, start=1)
    ]


def discover_profile_sources(profile: str) -> list[WeComDocSource]:
    normalized = normalize_env_profile(profile)
    docids: list[str] = []
    docids.extend(profiled_env_values("DOCID", "WEDOC", normalized))
    docids.extend(profiled_env_values("ID", "SMARTSHEET", normalized))
    docids = list(dict.fromkeys(item.strip() for item in docids if _looks_like_docid(item)))
    configured_name = (
        get_profiled_env("NAME", "SMARTSHEET", normalized)
        or get_profiled_env("NAME", "WEDOC", normalized)
        or get_profiled_env("TITLE", "SMARTSHEET", normalized)
        or get_profiled_env("TITLE", "WEDOC", normalized)
    )

    sources: list[WeComDocSource] = []
    for index, docid in enumerate(docids, start=1):
        sources.append(
            WeComDocSource(
                env_profile=normalized,
                docid=docid,
                source_name=configured_name or f"{normalized} 智能表格 {index}",
            )
        )
    return sources


def _looks_like_docid(value: str) -> bool:
    text = str(value or "").strip()
    return (text.startswith("dc") or text.startswith("s2_") or text.startswith("s3_")) and len(text) > 20


def sanitize_url(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    if not parts.scheme or not parts.netloc:
        return str(url or "").strip()
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in SENSITIVE_URL_PARAMS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def parse_smartsheet_link(url: str) -> dict[str, str]:
    parts = urlsplit(str(url or "").strip())
    path_parts = [item for item in parts.path.split("/") if item]
    docid = ""
    for item in path_parts:
        if item.startswith(("dc", "s2_", "s3_")):
            docid = item
            break
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    return {
        "docid": docid or query.get("docid", ""),
        "sheet_id": query.get("sheet_id") or query.get("tab") or query.get("sheetid") or "",
        "view_id": query.get("viewId") or query.get("view_id") or "",
        "source_url": sanitize_url(url),
    }


def summarize_wecom_error(error_text: str) -> dict[str, Any]:
    errcodes: dict[str, int] = {}
    from_ips: dict[str, int] = {}
    for errcode in re.findall(r"errcode['\"]?:\s*(\d+)", error_text):
        errcodes[errcode] = errcodes.get(errcode, 0) + 1
    for from_ip in re.findall(r"from ip:\s*([0-9.]+)", error_text):
        from_ips[from_ip] = from_ips.get(from_ip, 0) + 1
    return {"errcodes": errcodes, "from_ips": from_ips}


class WeComApiError(RuntimeError):
    """企微接口返回非 0 errcode。保留原有 str 形式，另挂 errcode/errmsg 供调用方分流。"""

    def __init__(self, path: str, data: dict[str, Any]) -> None:
        self.errcode = data.get("errcode")
        self.errmsg = data.get("errmsg")
        super().__init__(f"{path} failed: {data}")


class WeComSmartsheetClient:
    def __init__(self, corpid: str, secret: str, timeout: int = 15, use_system_proxy: bool = False) -> None:
        self.corpid = corpid
        self.secret = secret
        self.timeout = timeout
        self._token: str | None = None
        self.session = requests.Session()
        self.session.trust_env = use_system_proxy

    @staticmethod
    def _redact_url(url: str) -> str:
        parts = urlsplit(url)
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query.append((key, "***" if key.lower() in {"corpsecret", "access_token"} else value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, NETWORK_RETRIES + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except RequestException as exc:
                last_error = exc
                if attempt < NETWORK_RETRIES:
                    time.sleep(RETRY_SLEEP_SECONDS * attempt)
                    continue
            except ValueError as exc:
                raise RuntimeError(f"企业微信接口返回的不是 JSON：{self._redact_url(url)}") from exc

        raise RuntimeError(
            "企业微信接口连接失败，可能是网络、代理或远端临时断开。"
            f" 请求地址：{self._redact_url(url)}"
            f" 原始错误类型：{type(last_error).__name__ if last_error else 'unknown'}"
        ) from last_error

    def access_token(self) -> str:
        if self._token:
            return self._token
        data = self._request_json(
            "GET",
            f"{BASE}/gettoken",
            params={"corpid": self.corpid, "corpsecret": self.secret},
        )
        if data.get("errcode") != 0:
            raise RuntimeError(f"gettoken failed: {data}")
        self._token = str(data["access_token"])
        return self._token

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._request_json(
            "POST",
            f"{BASE}{path}",
            params={"access_token": self.access_token()},
            json=payload,
        )
        if data.get("errcode") != 0:
            raise WeComApiError(path, data)
        return data

    def get_fields(self, docid: str, sheet_id: str) -> dict[str, Any]:
        return self._post("/wedoc/smartsheet/get_fields", {"docid": docid, "sheet_id": sheet_id})

    def get_doc_base(self, docid: str) -> dict[str, str]:
        """实时取文档名与 modify_time（增量跳过依据）。失败返回空值，不影响同步本身。"""
        try:
            data = self._post("/wedoc/get_doc_base_info", {"docid": docid})
        except Exception:  # noqa: BLE001
            return {"doc_name": "", "modify_time": ""}
        info = data.get("doc_base_info")
        if not isinstance(info, dict):
            info = data
        return {
            "doc_name": str(info.get("doc_name") or "").strip(),
            "modify_time": str(info.get("modify_time") or "").strip(),
        }

    def get_doc_name(self, docid: str) -> str:
        return self.get_doc_base(docid)["doc_name"]

    def get_sheets(self, docid: str) -> list[dict[str, Any]]:
        data = self._post("/wedoc/smartsheet/get_sheet", {"docid": docid})
        sheets = data.get("sheets") or data.get("sheet_list") or data.get("data") or []
        if isinstance(sheets, dict):
            sheets = sheets.get("sheets") or sheets.get("sheet_list") or []
        return sheets if isinstance(sheets, list) else []

    def _record_page(self, docid: str, sheet_id: str, offset: int, limit: int) -> dict[str, Any]:
        payload: dict[str, Any] = {"docid": docid, "sheet_id": sheet_id, "limit": limit}
        if offset:
            payload["offset"] = offset
        return self._post("/wedoc/smartsheet/get_records", payload)

    def _scan_record_page(
        self, docid: str, sheet_id: str, offset: int, limit: int
    ) -> tuple[list[Any], list[int], bool]:
        """整页读不出时逐条重试，返回（可读记录, 不可读记录的 1-based 序号, 是否已到表尾）。"""
        records: list[Any] = []
        unreadable: list[int] = []
        for index in range(offset, offset + limit):
            try:
                page = self._record_page(docid, sheet_id, index, 1)
            except WeComApiError as exc:
                if exc.errcode not in UNREADABLE_RECORD_ERRCODES:
                    raise
                unreadable.append(index + 1)
                continue
            got = list(page.get("records") or [])
            if not got:
                return records, unreadable, True
            records.extend(got)
        return records, unreadable, False

    def get_records(self, docid: str, sheet_id: str) -> dict[str, Any]:
        """按 offset 分页拉全量记录。

        企微 get_records 只认请求里的 offset；**响应里的 next 回传给服务端会被忽略**，
        带 next 翻页会反复拿回同一页且 has_more 恒为 true（2026-08-28 实测，连发 8 次全同），
        旧写法因此是个死循环——只是不传 limit 时服务端一次返回整表、翻页分支从未走到才没爆。
        单条记录不可读时整页失败，此时把该页降级成逐条拉取，跳过读不出的那几条，
        并把它们的序号回传给调用方计数。
        """
        records: list[Any] = []
        unreadable: list[int] = []
        last_page: dict[str, Any] = {}
        offset = 0
        page_count = 0

        while page_count < MAX_RECORD_PAGES:
            try:
                page = self._record_page(docid, sheet_id, offset, RECORD_PAGE_SIZE)
            except WeComApiError as exc:
                if exc.errcode not in UNREADABLE_RECORD_ERRCODES:
                    raise
                got, skipped, exhausted = self._scan_record_page(docid, sheet_id, offset, RECORD_PAGE_SIZE)
                records.extend(got)
                unreadable.extend(skipped)
                page_count += 1
                if exhausted:
                    break
                offset += RECORD_PAGE_SIZE
                continue
            page_count += 1
            last_page = page
            got = list(page.get("records") or [])
            records.extend(got)
            if not page.get("has_more") or not got:
                break
            offset += len(got)
        else:
            raise RuntimeError(f"企业微信 get_records 翻页超过 {MAX_RECORD_PAGES} 页，疑似未收敛。")

        merged = dict(last_page)
        merged["records"] = records
        merged["fetched_count"] = len(records)
        merged["page_count"] = page_count
        merged["unreadable_offsets"] = unreadable
        merged["unreadable_count"] = len(unreadable)
        return merged

    def update_records(self, docid: str, sheet_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post("/wedoc/smartsheet/update_records", {"docid": docid, "sheet_id": sheet_id, "records": records})

    def add_records(self, docid: str, sheet_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post("/wedoc/smartsheet/add_records", {"docid": docid, "sheet_id": sheet_id, "records": records})

    def add_sheet(self, docid: str, title: str, index: int | None = None) -> dict[str, Any]:
        properties: dict[str, Any] = {"title": title}
        if index is not None:
            properties["index"] = index
        return self._post("/wedoc/smartsheet/add_sheet", {"docid": docid, "properties": properties})

    def add_fields(self, docid: str, sheet_id: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post("/wedoc/smartsheet/add_fields", {"docid": docid, "sheet_id": sheet_id, "fields": fields})

    def update_fields(self, docid: str, sheet_id: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post("/wedoc/smartsheet/update_fields", {"docid": docid, "sheet_id": sheet_id, "fields": fields})

    def upload_image(self, docid: str, content: bytes) -> str:
        # 走文档专用上传（/wedoc/image_upload），落到文档 CDN（wdcdn.qpic.cn）并自带尺寸，
        # 客户端可内联直显；不要用通用 /media/uploadimg（返回 wework.qpic.cn 外链、无尺寸、需点击加载）。
        data = self._post(
            "/wedoc/image_upload",
            {"docid": docid, "base64_content": base64.b64encode(content).decode("ascii")},
        )
        url = str(data.get("url") or "").strip()
        if not url:
            raise RuntimeError(f"/wedoc/image_upload missing url: {data}")
        return url
