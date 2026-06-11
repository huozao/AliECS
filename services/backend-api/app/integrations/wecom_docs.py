"""企业微信智能表格副本创建（结构 + 全部记录）。

backend 直调企微 API（urllib，无新依赖）。凭证来自 WECOM_{PROFILE}_CORP_ID/APP_SECRET，
文档管理员来自 WECOM_DOC_ADMIN_USERS（创建文档必填）。
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib import parse, request


BASE = "https://qyapi.weixin.qq.com/cgi-bin"
RECORD_BATCH_SIZE = 50
# 回写记录时，这些键形态的值属于"复杂元素"（图片/附件等），整条失败后剔除重试。
_COMPLEX_VALUE_KEYS = ("image_url", "file_url", "download_url")


class WeComDocError(RuntimeError):
    pass


def _request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    with request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8")
    result = json.loads(body) if body else {}
    if not isinstance(result, dict):
        raise WeComDocError("企业微信接口返回非对象 JSON")
    return result


def credentials_for_profile(env_profile: str) -> tuple[str, str]:
    profile = str(env_profile or "").strip().upper()
    corpid = os.getenv(f"WECOM_{profile}_CORP_ID", "").strip()
    secret = os.getenv(f"WECOM_{profile}_APP_SECRET", "").strip()
    if not corpid or not secret:
        raise WeComDocError(f"缺少 WECOM_{profile}_CORP_ID / WECOM_{profile}_APP_SECRET 配置")
    return corpid, secret


def doc_admin_users() -> list[str]:
    raw = os.getenv("WECOM_DOC_ADMIN_USERS", "")
    users = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    if not users:
        raise WeComDocError("缺少 WECOM_DOC_ADMIN_USERS 配置（创建副本需要文档管理员 userid）")
    return users


class WeComDocClient:
    def __init__(self, corpid: str, secret: str) -> None:
        self.corpid = corpid
        self.secret = secret
        self._token: str | None = None

    def access_token(self) -> str:
        if self._token:
            return self._token
        query = parse.urlencode({"corpid": self.corpid, "corpsecret": self.secret})
        data = _request_json("GET", f"{BASE}/gettoken?{query}")
        if data.get("errcode") != 0:
            raise WeComDocError(f"gettoken failed: {data}")
        self._token = str(data["access_token"])
        return self._token

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        query = parse.urlencode({"access_token": self.access_token()})
        data = _request_json("POST", f"{BASE}{path}?{query}", payload)
        if data.get("errcode") != 0:
            raise WeComDocError(f"{path} failed: {data}")
        return data

    def get_sheets(self, docid: str) -> list[dict[str, Any]]:
        data = self.post("/wedoc/smartsheet/get_sheet", {"docid": docid})
        sheets = data.get("sheets") or data.get("sheet_list") or []
        return sheets if isinstance(sheets, list) else []

    def get_fields(self, docid: str, sheet_id: str) -> list[dict[str, Any]]:
        data = self.post("/wedoc/smartsheet/get_fields", {"docid": docid, "sheet_id": sheet_id})
        fields = data.get("fields") or data.get("field_list") or []
        return fields if isinstance(fields, list) else []

    def get_records(self, docid: str, sheet_id: str) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"docid": docid, "sheet_id": sheet_id}
        page = self.post("/wedoc/smartsheet/get_records", payload)
        records = list(page.get("records") or [])
        cursor = page.get("next")
        while page.get("has_more") and cursor not in (None, ""):
            page = self.post("/wedoc/smartsheet/get_records", {**payload, "next": cursor})
            records.extend(page.get("records") or [])
            cursor = page.get("next")
        return records

    def create_doc(self, doc_name: str, admin_users: list[str]) -> str:
        data = self.post("/wedoc/create_doc", {"doc_type": 10, "doc_name": doc_name, "admin_users": admin_users})
        docid = str(data.get("docid") or "")
        if not docid:
            raise WeComDocError(f"create_doc 未返回 docid: {data}")
        return docid

    def add_sheet(self, docid: str, title: str, index: int) -> None:
        self.post("/wedoc/smartsheet/add_sheet", {"docid": docid, "properties": {"title": title, "index": index}})

    def delete_sheets(self, docid: str, sheet_ids: list[str]) -> None:
        for sheet_id in sheet_ids:
            try:
                self.post("/wedoc/smartsheet/delete_sheet", {"docid": docid, "sheet_id": sheet_id})
            except WeComDocError:
                pass  # 默认表删不掉就留着，不影响副本内容。

    def add_fields(self, docid: str, sheet_id: str, fields: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        """返回 (创建成功的字段标题, 失败被跳过的字段标题)。"""
        created: list[str] = []
        skipped: list[str] = []
        cleaned = [_strip_field(field) for field in fields]
        cleaned = [field for field in cleaned if field.get("field_title")]
        try:
            self.post("/wedoc/smartsheet/add_fields", {"docid": docid, "sheet_id": sheet_id, "fields": cleaned})
            return [str(field["field_title"]) for field in cleaned], skipped
        except WeComDocError:
            pass
        for field in cleaned:
            title = str(field.get("field_title"))
            try:
                self.post("/wedoc/smartsheet/add_fields", {"docid": docid, "sheet_id": sheet_id, "fields": [field]})
                created.append(title)
            except WeComDocError:
                skipped.append(title)
        return created, skipped

    def add_records(
        self,
        docid: str,
        sheet_id: str,
        records: list[dict[str, Any]],
        allowed_titles: set[str] | None = None,
    ) -> tuple[int, int]:
        """按批回写记录，返回 (written, skipped)。values 先过滤到目标表实际存在的字段。"""
        written = 0
        skipped = 0
        for start in range(0, len(records), RECORD_BATCH_SIZE):
            batch = records[start : start + RECORD_BATCH_SIZE]
            payload_records = [
                {"values": _filter_values(record.get("values") or {}, allowed_titles)} for record in batch
            ]
            payload_records = [record for record in payload_records if record["values"]]
            if not payload_records:
                skipped += len(batch)
                continue
            try:
                self.post(
                    "/wedoc/smartsheet/add_records",
                    {
                        "docid": docid,
                        "sheet_id": sheet_id,
                        "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
                        "records": payload_records,
                    },
                )
                written += len(batch)
                continue
            except WeComDocError:
                pass
            for record in payload_records:
                if _write_single_record(self, docid, sheet_id, record):
                    written += 1
                else:
                    skipped += 1
        return written, skipped


def _write_single_record(client: WeComDocClient, docid: str, sheet_id: str, record: dict[str, Any]) -> bool:
    base = {"docid": docid, "sheet_id": sheet_id, "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE"}
    try:
        client.post("/wedoc/smartsheet/add_records", {**base, "records": [record]})
        return True
    except WeComDocError:
        pass
    simplified = {"values": strip_complex_values(record.get("values") or {})}
    if simplified["values"] == record.get("values"):
        return False
    try:
        client.post("/wedoc/smartsheet/add_records", {**base, "records": [simplified]})
        return True
    except WeComDocError:
        return False


def _strip_field(field: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in field.items() if key != "field_id"}


def _filter_values(values: dict[str, Any], allowed_titles: set[str] | None) -> dict[str, Any]:
    if allowed_titles is None:
        return dict(values)
    return {title: value for title, value in values.items() if title in allowed_titles}


def strip_complex_values(values: dict[str, Any]) -> dict[str, Any]:
    """剔除图片/附件等回写不兼容的复杂值字段。"""
    cleaned: dict[str, Any] = {}
    for title, value in values.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            if any(key in value[0] for key in _COMPLEX_VALUE_KEYS):
                continue
        cleaned[title] = value
    return cleaned


def copy_smartsheet_doc(
    env_profile: str,
    source_docid: str,
    new_doc_name: str,
) -> dict[str, Any]:
    corpid, secret = credentials_for_profile(env_profile)
    admin_users = doc_admin_users()
    client = WeComDocClient(corpid, secret)

    source_sheets = client.get_sheets(source_docid)
    if not source_sheets:
        raise WeComDocError("源文档没有可复制的工作表")

    new_docid = client.create_doc(new_doc_name, admin_users)
    default_sheet_ids = [str(s.get("sheet_id") or "") for s in client.get_sheets(new_docid) if s.get("sheet_id")]

    sheets_created = 0
    records_written = 0
    records_skipped = 0
    warnings: list[str] = []

    for index, sheet in enumerate(source_sheets, start=1):
        sheet_id = str(sheet.get("sheet_id") or "")
        title = str((sheet.get("properties") or {}).get("title") or sheet.get("title") or f"Sheet{index}")
        if not sheet_id:
            continue
        client.add_sheet(new_docid, title, index)
        new_sheet_id = ""
        for candidate in client.get_sheets(new_docid):
            candidate_title = str((candidate.get("properties") or {}).get("title") or candidate.get("title") or "")
            candidate_id = str(candidate.get("sheet_id") or "")
            if candidate_title == title and candidate_id not in default_sheet_ids:
                new_sheet_id = candidate_id
        if not new_sheet_id:
            warnings.append(f"工作表 {title} 创建后未定位到 sheet_id，跳过")
            continue
        sheets_created += 1

        fields = client.get_fields(source_docid, sheet_id)
        created_fields, skipped_fields = client.add_fields(new_docid, new_sheet_id, fields)
        if skipped_fields:
            warnings.append(f"{title}: 字段跳过 {', '.join(skipped_fields)}")

        try:
            records = client.get_records(source_docid, sheet_id)
        except WeComDocError as exc:
            warnings.append(f"{title}: 读取记录失败 {exc}")
            continue
        if records:
            written, skipped = client.add_records(new_docid, new_sheet_id, records, allowed_titles=set(created_fields))
            records_written += written
            records_skipped += skipped
            if skipped:
                warnings.append(f"{title}: {skipped} 条记录回写失败已跳过")

    client.delete_sheets(new_docid, default_sheet_ids)
    return {
        "new_docid": new_docid,
        "url": f"https://doc.weixin.qq.com/smartsheet/{new_docid}",
        "sheets_created": sheets_created,
        "records_written": records_written,
        "records_skipped": records_skipped,
        "warnings": warnings,
    }
