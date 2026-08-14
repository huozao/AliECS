from __future__ import annotations

import json
import os
from typing import Any

from app.pipelines.wecom_structure_backup import (
    _field_items,
    _initialize_sheet_fields,
    _record_index,
    _sheet_identity,
    structure_backup_admin_users,
    structure_backup_enabled,
)
from app.providers.wecom import WeComSmartsheetClient, credentials_for_profile, normalize_env_profile
from app.storage.postgres import _redact_locator_error, open_store


CURRENT_SHEET = "文档定位档案"
EVENT_SHEET = "定位档案变更历史"
CURRENT_FIELDS = (
    "平台",
    "企业配置",
    "文档名称",
    "文档定位ID",
    "来源链接",
    "管理员",
    "凭据引用",
    "来源类型",
    "生命周期状态",
    "可同步状态",
    "不可同步原因",
    "可读",
    "可写",
    "可创建副本",
    "工作表数量",
    "登记时间",
    "最后验证时间",
    "最后同步时间",
    "最后更新时间",
    "唯一键",
    "最近错误",
)
EVENT_FIELDS = (
    "事件时间",
    "文档名称",
    "事件类型",
    "触发来源",
    "变更字段",
    "状态摘要",
    "唯一键",
)


class DocumentLocatorMirrorError(RuntimeError):
    pass


def _ensure_fields(client: Any, docid: str, sheet_id: str, titles: tuple[str, ...], *, created: bool) -> None:
    if created:
        _initialize_sheet_fields(client, docid, sheet_id, list(titles))
        return
    existing = {
        str(field.get("field_title") or field.get("title") or field.get("name") or "")
        for field in _field_items(client.get_fields(docid, sheet_id))
    }
    missing = [title for title in titles if title not in existing]
    for start in range(0, len(missing), 20):
        client.add_fields(
            docid,
            sheet_id,
            [{"field_title": title, "field_type": "FIELD_TYPE_TEXT"} for title in missing[start : start + 20]],
        )


def ensure_locator_workbook(client: Any, *, docid: str) -> dict[str, Any]:
    if not str(docid or "").strip():
        raise DocumentLocatorMirrorError("缺少 WECOM_STRUCTURE_BACKUP_DOCID。")
    existing = {
        title: sheet_id
        for sheet_id, title in map(_sheet_identity, client.get_sheets(docid))
        if sheet_id and title
    }
    created_titles: set[str] = set()
    for index, title in enumerate((CURRENT_SHEET, EVENT_SHEET), start=1):
        if title not in existing:
            client.add_sheet(docid, title, index)
            created_titles.add(title)
            existing = {
                name: sheet_id
                for sheet_id, name in map(_sheet_identity, client.get_sheets(docid))
                if sheet_id and name
            }
        if title not in existing:
            raise DocumentLocatorMirrorError(f"定位镜像工作表创建后未找到：{title}")
    _ensure_fields(client, docid, existing[CURRENT_SHEET], CURRENT_FIELDS, created=CURRENT_SHEET in created_titles)
    _ensure_fields(client, docid, existing[EVENT_SHEET], EVENT_FIELDS, created=EVENT_SHEET in created_titles)
    return {"docid": docid, "sheets": {CURRENT_SHEET: existing[CURRENT_SHEET], EVENT_SHEET: existing[EVENT_SHEET]}}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _capability(locator: dict[str, Any], name: str) -> str:
    return str((locator.get("capabilities") or {}).get(name) or "unknown")


def _current_values(locator: dict[str, Any]) -> dict[str, str]:
    api_doc_id = str(locator.get("api_doc_id") or "")
    share_ref = str(locator.get("share_ref") or "")
    return {
        "平台": "企微" if locator.get("provider") == "wecom" else "飞书",
        "企业配置": _text(locator.get("env_profile")),
        "文档名称": _text(locator.get("document_name")),
        "文档定位ID": api_doc_id or share_ref,
        "来源链接": _text(locator.get("source_url")),
        "管理员": "; ".join(str(item) for item in locator.get("admin_userids") or []),
        "凭据引用": _text(locator.get("credential_ref")),
        "来源类型": _text(locator.get("source_kind")),
        "生命周期状态": _text(locator.get("lifecycle_status")),
        "可同步状态": _text(locator.get("syncability_status")),
        "不可同步原因": _text(locator.get("last_error_summary")),
        "可读": _capability(locator, "read"),
        "可写": _capability(locator, "write"),
        "可创建副本": _capability(locator, "copy"),
        "工作表数量": _text(locator.get("sheet_count")),
        "登记时间": _text(locator.get("registered_at")),
        "最后验证时间": _text(locator.get("last_verified_at")),
        "最后同步时间": _text(locator.get("last_sync_at")),
        "最后更新时间": _text(locator.get("updated_at")),
        "唯一键": f"locator:{int(locator['id'])}",
        "最近错误": _text(locator.get("last_error_summary")),
    }


def _event_values(payload: dict[str, Any]) -> dict[str, str] | None:
    locator = payload["locator"]
    event = payload.get("event") or {}
    if not event.get("event_type"):
        return None
    event_key = ":".join(
        (
            f"locator:{int(locator['id'])}",
            str(event.get("created_at") or ""),
            str(event.get("event_type") or ""),
        )
    )
    return {
        "事件时间": _text(event.get("created_at")),
        "文档名称": _text(locator.get("document_name")),
        "事件类型": _text(event.get("event_type")),
        "触发来源": _text(event.get("trigger_source")),
        "变更字段": "; ".join(str(item) for item in event.get("changed_fields") or []),
        "状态摘要": _text(event.get("status_summary") or {}),
        "唯一键": event_key,
    }


def write_locator_mirror(
    client: Any,
    *,
    backup_docid: str,
    sheet_ids: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, bool]:
    current_sheet_id = sheet_ids[CURRENT_SHEET]
    current_values = _current_values(payload["locator"])
    current_index = _record_index(client, backup_docid, current_sheet_id, "唯一键")
    current = current_index.get(current_values["唯一键"])
    if current:
        record_id = str(current.get("record_id") or current.get("id") or "")
        if not record_id:
            raise DocumentLocatorMirrorError("定位档案当前行缺少 record_id。")
        client.update_records(backup_docid, current_sheet_id, [{"record_id": record_id, "values": current_values}])
    else:
        client.add_records(backup_docid, current_sheet_id, [{"values": current_values}])

    event_added = False
    event_values = _event_values(payload)
    if event_values:
        event_sheet_id = sheet_ids[EVENT_SHEET]
        event_index = _record_index(client, backup_docid, event_sheet_id, "唯一键")
        if event_values["唯一键"] not in event_index:
            client.add_records(backup_docid, event_sheet_id, [{"values": event_values}])
            event_added = True
    return {"current_written": True, "event_added": event_added}


def _workbook_client() -> tuple[WeComSmartsheetClient, dict[str, Any]]:
    profile = normalize_env_profile(os.getenv("WECOM_STRUCTURE_BACKUP_PROFILE", "COMPANY_A"))
    docid = str(os.getenv("WECOM_STRUCTURE_BACKUP_DOCID", "")).strip()
    credentials = credentials_for_profile(profile)
    if not credentials:
        raise DocumentLocatorMirrorError(f"{profile} 缺少企业微信自建应用凭据。")
    errors: list[str] = []
    for credential in credentials:
        client = WeComSmartsheetClient(credential.corpid, credential.secret)
        try:
            return client, ensure_locator_workbook(client, docid=docid)
        except Exception as exc:  # noqa: BLE001 - try each configured credential without leaking values.
            errors.append(f"{credential.label}:{type(exc).__name__}")
    raise DocumentLocatorMirrorError("所有结构备份凭据均无法访问定位档案文档：" + " | ".join(errors))


def run_pending_document_locator_mirror_jobs(*, limit: int = 10, force: bool = False) -> int:
    if not force and not structure_backup_enabled():
        return 0
    store = open_store()
    jobs: list[dict[str, Any]] = []
    exit_code = 0
    try:
        jobs = store.claim_document_locator_mirror_jobs(limit=limit)
        if not jobs:
            return 0
        try:
            client, workbook = _workbook_client()
        except Exception as exc:  # noqa: BLE001 - durable jobs retain the work for retry.
            safe_error = _redact_locator_error(str(exc))
            for job in jobs:
                delay = min(3600, 60 * (2 ** int(job.get("attempt_count") or 0)))
                store.retry_document_locator_mirror_job(int(job["id"]), safe_error, delay)
            return 1
        for job in jobs:
            job_id = int(job["id"])
            try:
                payload = store.get_document_locator_mirror_payload(
                    int(job["locator_id"]),
                    int(job["locator_version"]),
                )
                if not payload:
                    raise DocumentLocatorMirrorError("定位档案镜像任务找不到内部记录。")
                write_locator_mirror(
                    client,
                    backup_docid=str(workbook["docid"]),
                    sheet_ids=dict(workbook["sheets"]),
                    payload=payload,
                )
                store.finish_document_locator_mirror_job(job_id)
            except Exception as exc:  # noqa: BLE001 - one document must not block other mirror jobs.
                delay = min(3600, 60 * (2 ** int(job.get("attempt_count") or 0)))
                store.retry_document_locator_mirror_job(job_id, _redact_locator_error(str(exc)), delay)
                exit_code = 1
    finally:
        store.close()
    return exit_code
