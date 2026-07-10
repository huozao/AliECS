from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from app.providers.wecom import WeComSmartsheetClient, credentials_for_profile, normalize_env_profile
from app.storage.postgres import open_store, stable_hash


BACKUP_DOC_NAME = "企微智能表格结构备份"
BACKUP_SHEET_TITLES = ("企微A-最新结构", "企微B-最新结构", "飞书-最新结构", "结构变更历史")
COMMON_FIELD_TITLES = (
    "平台",
    "来源类型",
    "智能表格名称",
    "工作表数量",
    "字段总数",
    "来源链接",
    "企业配置",
    "状态",
    "文档定位ID",
    "唯一键",
    "创建来源",
    "创建副本请求时间",
    "企微修改时间",
    "源同步时间",
    "备份更新时间",
    "结构最后变化时间",
    "结构哈希",
)
HISTORY_FIELD_TITLES = ("版本唯一键", "上一结构哈希", "触发来源", "结构变化时间")
FIELD_BATCH_SIZE = 20


class StructureBackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentStructureSnapshot:
    source_id: int
    env_profile: str
    docid: str
    document_name: str
    unique_key: str
    structure_hash: str
    values: dict[str, Any]
    structure: dict[str, Any]
    platform: str
    target_sheet_title: str


def backup_field_titles(sheet_title: str, *, max_sheets: int) -> list[str]:
    titles = list(COMMON_FIELD_TITLES)
    if sheet_title == "结构变更历史":
        titles.extend(HISTORY_FIELD_TITLES)
    for index in range(1, max_sheets + 1):
        prefix = f"工作表{index:02d}"
        titles.extend(
            (
                f"{prefix}名称",
                f"{prefix}编码",
                f"{prefix}类型",
                f"{prefix}可见性",
                f"{prefix}字段结构",
                f"{prefix}字段数量",
            )
        )
    return titles


def _sheet_identity(sheet: dict[str, Any]) -> tuple[str, str]:
    properties = sheet.get("properties") if isinstance(sheet.get("properties"), dict) else {}
    sheet_id = str(sheet.get("sheet_id") or sheet.get("id") or "")
    title = str(properties.get("title") or sheet.get("title") or sheet.get("name") or "")
    return sheet_id, title


def _field_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    fields = response.get("fields") or response.get("field_list") or []
    return fields if isinstance(fields, list) else []


def _record_values_by_title(
    records: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    target_titles: list[str],
) -> list[dict[str, Any]]:
    id_to_title = {
        str(field.get("field_id") or field.get("id") or ""): str(
            field.get("field_title") or field.get("title") or field.get("name") or ""
        )
        for field in fields
    }
    allowed = set(target_titles)
    copied: list[dict[str, Any]] = []
    for record in records:
        values = record.get("values") if isinstance(record.get("values"), dict) else {}
        title_values: dict[str, Any] = {}
        for key, value in values.items():
            title = str(key) if str(key) in allowed else id_to_title.get(str(key), "")
            if title in allowed:
                title_values[title] = value
        copied.append({"values": title_values})
    return copied


def _initialize_sheet_fields(
    client: Any,
    docid: str,
    sheet_id: str,
    target_titles: list[str],
) -> None:
    existing = _field_items(client.get_fields(docid, sheet_id))
    remaining = list(target_titles)
    if existing and remaining:
        first = existing[0]
        first_id = str(first.get("field_id") or first.get("id") or "")
        first_type = str(first.get("field_type") or first.get("type") or "FIELD_TYPE_TEXT")
        if first_id and str(first.get("field_title") or first.get("title") or "") != remaining[0]:
            client.update_fields(
                docid,
                sheet_id,
                [{"field_id": first_id, "field_title": remaining[0], "field_type": first_type}],
            )
        remaining = remaining[1:]
    field_defs = [{"field_title": name, "field_type": "FIELD_TYPE_TEXT"} for name in remaining]
    for start in range(0, len(field_defs), FIELD_BATCH_SIZE):
        batch = field_defs[start : start + FIELD_BATCH_SIZE]
        client.add_fields(docid, sheet_id, list(reversed(batch)))


def rebuild_sheet_with_order(
    client: Any,
    *,
    docid: str,
    sheet_id: str,
    sheet_title: str,
    target_titles: list[str],
    index: int,
) -> str:
    old_fields = _field_items(client.get_fields(docid, sheet_id))
    old_records = list(client.get_records(docid, sheet_id).get("records") or [])
    copied_records = _record_values_by_title(old_records, old_fields, target_titles)
    temporary_title = f"__迁移{index}_{sheet_title}"
    client.add_sheet(docid, temporary_title, index)
    candidates = {
        title: candidate_id
        for candidate_id, title in map(_sheet_identity, client.get_sheets(docid))
        if candidate_id and candidate_id != sheet_id
    }
    new_sheet_id = candidates.get(temporary_title, "")
    if not new_sheet_id:
        raise StructureBackupError(f"{sheet_title} 迁移表创建后未定位到 sheet_id。")
    _initialize_sheet_fields(client, docid, new_sheet_id, target_titles)
    for start in range(0, len(copied_records), 50):
        client.add_records(docid, new_sheet_id, copied_records[start : start + 50])
    written_count = len(client.get_records(docid, new_sheet_id).get("records") or [])
    if written_count != len(old_records):
        raise StructureBackupError(
            f"{sheet_title} 迁移记录数不一致：旧表 {len(old_records)}，新表 {written_count}。"
        )
    old_title = f"__旧{index}_{sheet_title}"
    client.update_sheet(docid, sheet_id, old_title)
    try:
        client.update_sheet(docid, new_sheet_id, sheet_title)
    except Exception:
        client.update_sheet(docid, sheet_id, sheet_title)
        raise
    client.delete_sheet(docid, sheet_id)
    return new_sheet_id


def ensure_backup_workbook(
    client: Any,
    *,
    docid: str,
    admin_users: list[str],
    max_sheets: int,
) -> dict[str, Any]:
    created = not str(docid or "").strip()
    if created:
        docid = client.create_doc(BACKUP_DOC_NAME, admin_users)
    initial_sheets = list(client.get_sheets(docid))
    initial_sheet_ids = [sheet_id for sheet_id, _ in map(_sheet_identity, initial_sheets) if sheet_id]

    existing = {title: sheet_id for sheet_id, title in map(_sheet_identity, initial_sheets) if sheet_id and title}
    created_titles: set[str] = set()
    for index, title in enumerate(BACKUP_SHEET_TITLES, start=1):
        if title not in existing:
            client.add_sheet(docid, title, index)
            created_titles.add(title)
            refreshed = {name: sheet_id for sheet_id, name in map(_sheet_identity, client.get_sheets(docid))}
            if not refreshed.get(title):
                raise StructureBackupError(f"工作表 {title} 创建后未能定位 sheet_id。")
            existing = refreshed

    result_sheets = {title: existing[title] for title in BACKUP_SHEET_TITLES}
    for title in BACKUP_SHEET_TITLES:
        sheet_id = result_sheets[title]
        target_titles = backup_field_titles(title, max_sheets=max_sheets)
        current_titles = [
            str(field.get("field_title") or field.get("title") or field.get("name") or "")
            for field in _field_items(client.get_fields(docid, sheet_id))
        ]
        if title in created_titles:
            _initialize_sheet_fields(client, docid, sheet_id, target_titles)
        else:
            # 既有工作表只补缺失列，不强制列序、不删多余列（如历史遗留的 docid 列）：
            # 企微 provider 没有删表/改表名能力，rebuild_sheet_with_order 在生产不可行，
            # 触发即在 add_sheet 后中途崩溃并把备份 job 卡死在 running。
            missing = [name for name in target_titles if name not in current_titles]
            field_defs = [{"field_title": name, "field_type": "FIELD_TYPE_TEXT"} for name in missing]
            for start in range(0, len(field_defs), FIELD_BATCH_SIZE):
                client.add_fields(docid, sheet_id, field_defs[start : start + FIELD_BATCH_SIZE])

    if created:
        for sheet_id in initial_sheet_ids:
            client.delete_sheet(docid, sheet_id)
    return {
        "docid": docid,
        "url": f"https://doc.weixin.qq.com/smartsheet/{docid}",
        "sheets": result_sheets,
        "created": created,
    }


def _normalized_field(field: dict[str, Any]) -> dict[str, Any]:
    raw = field.get("raw_json") if isinstance(field.get("raw_json"), dict) else {}
    field_id = str(raw.get("field_id") or raw.get("id") or field.get("external_field_id") or "")
    name = str(
        raw.get("field_title")
        or raw.get("field_name")
        or raw.get("title")
        or raw.get("name")
        or field.get("field_title")
        or ""
    )
    field_type = str(raw.get("field_type") or raw.get("type") or field.get("field_type") or "")
    config: dict[str, Any] = {}
    if "is_primary" in raw:
        config["is_primary"] = bool(raw["is_primary"])
    for key in sorted(raw):
        if key == "property" or key.startswith("property_"):
            value = raw.get(key)
            if value not in (None, "", [], {}):
                config[key] = value
    return {
        "id": field_id,
        "name": name,
        "type": field_type,
        "order": int(field.get("order") or 0),
        "config": config,
    }


def _normalized_sheet(sheet: dict[str, Any]) -> dict[str, Any]:
    fields = [_normalized_field(field) for field in sheet.get("fields") or [] if isinstance(field, dict)]
    fields.sort(key=lambda item: (item["order"], item["id"]))
    return {
        "id": str(sheet.get("external_sheet_id") or ""),
        "name": str(sheet.get("sheet_name") or ""),
        "type": str(sheet.get("source_type") or ""),
        "visibility": str(sheet.get("visibility") or ""),
        "fields": fields,
    }


def build_document_snapshot(
    source: dict[str, Any],
    sheets: list[dict[str, Any]],
    *,
    max_sheets: int,
) -> DocumentStructureSnapshot:
    if len(sheets) > max_sheets:
        raise StructureBackupError(f"智能表格包含 {len(sheets)} 张工作表，超过备份上限 {max_sheets}。")

    normalized_sheets = [_normalized_sheet(sheet) for sheet in sheets]
    normalized_sheets.sort(key=lambda item: item["id"])
    document_name = str(source.get("document_name") or "")
    structure = {"document_name": document_name, "sheets": normalized_sheets}
    structure_hash = stable_hash(structure)
    provider = str(source.get("provider") or "wecom")
    platform = "飞书" if provider == "feishu" else "企微"
    target_sheet_title = (
        "飞书-最新结构"
        if provider == "feishu"
        else ("企微A-最新结构" if str(source.get("env_profile") or "") == "COMPANY_A" else "企微B-最新结构")
    )
    env_profile = str(source.get("env_profile") or "")
    docid = str(source.get("external_doc_id") or "")
    source_url = str(source.get("source_url") or "")
    unique_key = f"FEISHU:{env_profile}:{docid}" if provider == "feishu" else f"{env_profile}:{docid}"
    values: dict[str, Any] = {
        "平台": platform,
        "唯一键": unique_key,
        "企业配置": env_profile,
        "智能表格名称": document_name,
        # ⚠️ 不要往 values 里写名为 "docid" 的键：企微服务端会把它误当本企业 docid 校验，
        # 跨企业 docid/空值一律 301085 invalid docid（源 docid 由「文档定位ID」列承载）。
        "文档定位ID": docid,
        "来源链接": source_url,
        "来源类型": str(source.get("source_type") or ""),
        "状态": str(source.get("status") or ""),
        "工作表数量": len(normalized_sheets),
        "字段总数": sum(len(sheet["fields"]) for sheet in normalized_sheets),
        "结构哈希": structure_hash,
        "企微修改时间": str(source.get("external_modified_at") or "") if provider == "wecom" else "",
        "源同步时间": str(source.get("last_sync_at") or ""),
        "备份更新时间": "",
        "结构最后变化时间": "",
        "创建来源": "copy-auto" if source.get("copy_requested_at") else str(source.get("source_type") or ""),
        "创建副本请求时间": str(source.get("copy_requested_at") or ""),
    }
    for index in range(1, max_sheets + 1):
        prefix = f"工作表{index:02d}"
        values.update(
            {
                f"{prefix}名称": "",
                f"{prefix}编码": "",
                f"{prefix}类型": "",
                f"{prefix}可见性": "",
                f"{prefix}字段结构": "",
                f"{prefix}字段数量": 0,
            }
        )
    for index, sheet in enumerate(normalized_sheets, start=1):
        prefix = f"工作表{index:02d}"
        values[f"{prefix}名称"] = sheet["name"]
        values[f"{prefix}编码"] = sheet["id"]
        values[f"{prefix}类型"] = sheet["type"]
        values[f"{prefix}可见性"] = sheet["visibility"]
        values[f"{prefix}字段结构"] = json.dumps(
            sheet["fields"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        values[f"{prefix}字段数量"] = len(sheet["fields"])

    return DocumentStructureSnapshot(
        source_id=int(source.get("id") or 0),
        env_profile=env_profile,
        docid=docid,
        document_name=document_name,
        unique_key=unique_key,
        structure_hash=structure_hash,
        values=values,
        structure=structure,
        platform=platform,
        target_sheet_title=target_sheet_title,
    )


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        texts = [_cell_text(item) for item in value]
        return "; ".join(item for item in texts if item)
    if isinstance(value, dict):
        for key in ("text", "value", "name"):
            if key in value:
                return str(value.get(key) or "")
        return ""
    return str(value)


def _record_index(client: Any, docid: str, sheet_id: str, key_title: str) -> dict[str, dict[str, Any]]:
    fields = _field_items(client.get_fields(docid, sheet_id))
    title_to_id = {
        str(field.get("field_title") or field.get("title") or field.get("name") or ""): str(
            field.get("field_id") or field.get("id") or ""
        )
        for field in fields
    }
    key_id = title_to_id.get(key_title, "")
    response = client.get_records(docid, sheet_id)
    records = response.get("records") or []
    indexed: dict[str, dict[str, Any]] = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        values = record.get("values") if isinstance(record.get("values"), dict) else {}
        key = _cell_text(values.get(key_title) if key_title in values else values.get(key_id))
        if key:
            normalized = {
                title: _cell_text(values.get(title) if title in values else values.get(field_id))
                for title, field_id in title_to_id.items()
            }
            indexed[key] = {**record, "title_values": normalized}
    return indexed


def _text_values(values: dict[str, Any]) -> dict[str, str]:
    return {key: "" if value is None else str(value) for key, value in values.items()}


def write_structure_snapshot(
    client: Any,
    *,
    backup_docid: str,
    sheet_ids: dict[str, str],
    snapshot: DocumentStructureSnapshot,
    trigger: str,
    now: str,
) -> dict[str, Any]:
    latest_title = snapshot.target_sheet_title
    if latest_title not in sheet_ids or "结构变更历史" not in sheet_ids:
        raise StructureBackupError(f"备份文档缺少 {latest_title} 或结构变更历史工作表。")

    latest_sheet_id = sheet_ids[latest_title]
    latest_records = _record_index(client, backup_docid, latest_sheet_id, "唯一键")
    current = latest_records.get(snapshot.unique_key)
    old_hash = str((current or {}).get("title_values", {}).get("结构哈希") or "")
    changed = old_hash != snapshot.structure_hash
    old_change_time = str((current or {}).get("title_values", {}).get("结构最后变化时间") or "")
    latest_values = _text_values(snapshot.values)
    latest_values["备份更新时间"] = now
    latest_values["结构最后变化时间"] = now if changed else old_change_time or now

    if current:
        record_id = str(current.get("record_id") or current.get("id") or "")
        if not record_id:
            raise StructureBackupError(f"最新结构行 {snapshot.unique_key} 缺少 record_id。")
        client.update_records(
            backup_docid,
            latest_sheet_id,
            [{"record_id": record_id, "values": latest_values}],
        )
    else:
        client.add_records(backup_docid, latest_sheet_id, [{"values": latest_values}])

    history_added = False
    version_key = f"{snapshot.unique_key}:{snapshot.structure_hash}"
    if changed:
        history_sheet_id = sheet_ids["结构变更历史"]
        history_records = _record_index(client, backup_docid, history_sheet_id, "版本唯一键")
        if version_key not in history_records:
            history_values = dict(latest_values)
            history_values.update(
                {
                    "版本唯一键": version_key,
                    "上一结构哈希": old_hash,
                    "触发来源": trigger,
                    "结构变化时间": now,
                }
            )
            client.add_records(backup_docid, history_sheet_id, [{"values": history_values}])
            history_added = True
    return {
        "changed": changed,
        "history_added": history_added,
        "unique_key": snapshot.unique_key,
        "structure_hash": snapshot.structure_hash,
    }


def structure_backup_enabled() -> bool:
    return str(os.getenv("WECOM_STRUCTURE_BACKUP_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def structure_backup_max_sheets() -> int:
    try:
        value = int(str(os.getenv("WECOM_STRUCTURE_BACKUP_MAX_SHEETS", "20")).strip())
    except ValueError:
        value = 20
    return value if value > 0 else 20


def structure_backup_admin_users() -> list[str]:
    raw = str(os.getenv("WECOM_DOC_ADMIN_USERS", ""))
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def refresh_backup_workbook_structure(
    store: Any,
    client: Any,
    *,
    workbook: dict[str, Any],
    profile: str,
) -> int:
    docid = str(workbook["docid"])
    source_url = str(workbook.get("url") or f"https://doc.weixin.qq.com/smartsheet/{docid}")
    document_source_id = store.upsert_structure_document(
        provider="wecom",
        env_profile=profile,
        source_type="structure_backup_doc",
        external_doc_id=docid,
        document_name=BACKUP_DOC_NAME,
        source_url=source_url,
    )
    for title, sheet_id in workbook["sheets"].items():
        source_id = store.ensure_source(
            provider="wecom",
            env_profile=profile,
            source_name=f"{BACKUP_DOC_NAME} / {title}",
            source_type="structure_backup_sheet",
            external_doc_id=docid,
            external_sheet_id=str(sheet_id),
            source_url=source_url,
            document_name=BACKUP_DOC_NAME,
            sheet_name=str(title),
        )
        store.replace_fields(source_id, _field_items(client.get_fields(docid, str(sheet_id))))
    store.deactivate_missing_structure_sheets(
        provider="wecom",
        env_profile=profile,
        external_doc_id=docid,
        active_sheet_ids=[str(sheet_id) for sheet_id in workbook["sheets"].values()],
    )
    return document_source_id


def enqueue_daily_structure_backup_jobs(
    store: Any,
    *,
    day: str | None = None,
    backup_docid: str = "",
) -> int:
    del backup_docid  # 保留参数兼容旧调用；备份文档自身现在也需要入队。
    event_day = day or date.today().isoformat()
    count = 0
    sources = list(store.list_wecom_document_structures())
    if hasattr(store, "list_feishu_document_structures"):
        sources.extend(store.list_feishu_document_structures())
    for source in sources:
        docid = str(source.get("external_doc_id") or "")
        if not docid:
            continue
        profile = str(source.get("env_profile") or "")
        provider = str(source.get("provider") or "wecom")
        event_key = (
            f"daily:{event_day}:FEISHU:{profile}:{docid}"
            if provider == "feishu"
            else f"daily:{event_day}:{profile}:{docid}"
        )
        store.enqueue_structure_backup_job(
            source_id=int(source["id"]),
            trigger="daily",
            event_key=event_key,
        )
        count += 1
    return count


def enqueue_copy_auto_structure_backup(store: Any, request: dict[str, Any], *, request_status: str) -> bool:
    if request_status != "success" or str(request.get("requested_by") or "") != "copy-auto":
        return False
    request_id = int(request["id"])
    source_id = int(request["source_id"])
    store.enqueue_structure_backup_job(
        source_id=source_id,
        trigger="copy-auto",
        event_key=f"copy-auto:{request_id}:{source_id}",
    )
    return True


def _workbook_client(
    *,
    profile: str,
    docid: str,
    admin_users: list[str],
    max_sheets: int,
) -> tuple[WeComSmartsheetClient, dict[str, Any]]:
    credentials = credentials_for_profile(profile)
    if not credentials:
        raise StructureBackupError(f"{profile} 缺少企业微信自建应用凭证。")
    errors: list[str] = []
    for credential in credentials:
        client = WeComSmartsheetClient(credential.corpid, credential.secret)
        try:
            workbook = ensure_backup_workbook(
                client,
                docid=docid,
                admin_users=admin_users,
                max_sheets=max_sheets,
            )
            return client, workbook
        except Exception as exc:  # noqa: BLE001 - 多个自建应用逐个尝试文档权限。
            errors.append(f"{credential.label}: {exc}")
    raise StructureBackupError("所有企微A自建应用均无法访问结构备份文档：" + " | ".join(errors))


def bootstrap_structure_backup(*, profile: str = "", docid: str = "") -> dict[str, Any]:
    backup_profile = normalize_env_profile(
        profile or os.getenv("WECOM_STRUCTURE_BACKUP_PROFILE", "COMPANY_A")
    )
    backup_docid = str(docid or os.getenv("WECOM_STRUCTURE_BACKUP_DOCID", "")).strip()
    admin_users = structure_backup_admin_users()
    if not backup_docid and not admin_users:
        raise StructureBackupError("首次创建结构备份文档需要 WECOM_DOC_ADMIN_USERS。")
    _, workbook = _workbook_client(
        profile=backup_profile,
        docid=backup_docid,
        admin_users=admin_users,
        max_sheets=structure_backup_max_sheets(),
    )
    return workbook


def run_enqueue_daily_structure_backup_jobs(*, force: bool = False) -> int:
    if not force and not structure_backup_enabled():
        return 0
    backup_docid = str(os.getenv("WECOM_STRUCTURE_BACKUP_DOCID", "")).strip()
    if not backup_docid:
        raise StructureBackupError("缺少 WECOM_STRUCTURE_BACKUP_DOCID。")
    store = open_store()
    try:
        backup_profile = normalize_env_profile(os.getenv("WECOM_STRUCTURE_BACKUP_PROFILE", "COMPANY_A"))
        client, workbook = _workbook_client(
            profile=backup_profile,
            docid=backup_docid,
            admin_users=structure_backup_admin_users(),
            max_sheets=structure_backup_max_sheets(),
        )
        refresh_backup_workbook_structure(store, client, workbook=workbook, profile=backup_profile)
        count = enqueue_daily_structure_backup_jobs(store, backup_docid=backup_docid)
        print(f"[企微结构备份] 已生成 {count} 个每日备份任务。")
        return count
    finally:
        store.close()


def run_pending_structure_backup_jobs(*, limit: int = 10, force: bool = False) -> int:
    if not force and not structure_backup_enabled():
        return 0
    backup_docid = str(os.getenv("WECOM_STRUCTURE_BACKUP_DOCID", "")).strip()
    if not backup_docid:
        raise StructureBackupError("缺少 WECOM_STRUCTURE_BACKUP_DOCID。")
    backup_profile = normalize_env_profile(os.getenv("WECOM_STRUCTURE_BACKUP_PROFILE", "COMPANY_A"))
    max_sheets = structure_backup_max_sheets()
    store = open_store()
    exit_code = 0
    try:
        jobs = store.claim_structure_backup_jobs(limit=limit)
        if not jobs:
            return 0
        client, workbook = _workbook_client(
            profile=backup_profile,
            docid=backup_docid,
            admin_users=structure_backup_admin_users(),
            max_sheets=max_sheets,
        )
        refresh_backup_workbook_structure(store, client, workbook=workbook, profile=backup_profile)
        for job in jobs:
            job_id = int(job["id"])
            try:
                source_meta = store.get_source(int(job["source_id"]))
                if not source_meta:
                    raise StructureBackupError(f"找不到 source_id={job['source_id']} 的结构来源。")
                if str(source_meta.get("provider") or "") == "feishu":
                    documents = store.list_feishu_document_structures(source_id=int(job["source_id"]))
                else:
                    documents = store.list_wecom_document_structures(source_id=int(job["source_id"]))
                if not documents:
                    raise StructureBackupError(f"找不到 source_id={job['source_id']} 的文档结构。")
                source = documents[0]
                snapshot = build_document_snapshot(source, source.get("sheets") or [], max_sheets=max_sheets)
                result = write_structure_snapshot(
                    client,
                    backup_docid=backup_docid,
                    sheet_ids=workbook["sheets"],
                    snapshot=snapshot,
                    trigger=str(job["trigger"]),
                    now=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                )
                store.finish_structure_backup_job(job_id)
                print(
                    f"[企微结构备份] {snapshot.unique_key} 完成，"
                    f"changed={result['changed']} history_added={result['history_added']}"
                )
            except Exception as exc:  # noqa: BLE001 - 持久化任务保留并指数退避重试。
                attempt = int(job.get("attempt_count") or 0)
                delay_seconds = min(3600, 60 * (2**attempt))
                store.retry_structure_backup_job(job_id, str(exc), delay_seconds=delay_seconds)
                exit_code = 1
                print(f"[企微结构备份] job_id={job_id} 失败，{delay_seconds}s 后重试：{exc}")
    finally:
        store.close()
    return exit_code
