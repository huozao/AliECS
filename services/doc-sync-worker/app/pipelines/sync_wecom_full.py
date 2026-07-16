from __future__ import annotations

from typing import Any

from app.providers.wecom import (
    WeComDocSource,
    WeComSmartsheetClient,
    credentials_for_profile,
    discover_profile_sources,
    env_profiles,
    summarize_wecom_error,
)
from app.pipelines.managed_contacts import CONTACT_SHEET_CHANNELS, SESSION_INDEX_SHEETS, sync_managed_contact_from_row
from app.pipelines.sync_feishu_full import sync_feishu_source
from app.pipelines.wecom_structure_backup import (
    enqueue_copy_auto_structure_backup,
    structure_backup_enabled,
)
from app.storage.postgres import build_record_snapshot, compose_source_name, open_store


def _sheet_id(sheet: dict[str, Any]) -> str:
    return str(sheet.get("sheet_id") or sheet.get("id") or sheet.get("sheetId") or "")


def _sheet_name(sheet: dict[str, Any]) -> str:
    return str(sheet.get("title") or sheet.get("name") or sheet.get("sheet_name") or _sheet_id(sheet) or "未命名 sheet")


def _fields_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    fields = response.get("fields") or response.get("field_list") or response.get("data") or []
    if isinstance(fields, dict):
        fields = fields.get("fields") or fields.get("field_list") or []
    return fields if isinstance(fields, list) else []


def _sync_sheet_records(
    store: Any,
    client: WeComSmartsheetClient,
    source_id: int,
    docid: str,
    sheet_id: str,
    counts: dict[str, int],
    sheet_name: str = "",
) -> None:
    fields_response = client.get_fields(docid, sheet_id)
    field_titles = store.replace_fields(source_id, _fields_from_response(fields_response))
    records_response = client.get_records(docid, sheet_id)
    records = records_response.get("records") or []
    counts["sheet_count"] += 1
    counts["record_count"] += len(records)
    print(
        f"[企业微信同步] docid={docid} sheet_id={sheet_id} "
        f"完整拉取 {len(records)} 条，分页 {records_response.get('page_count', 1)} 页。"
    )
    seen_record_ids: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        snapshot = build_record_snapshot(record, field_titles)
        seen_record_ids.append(snapshot.external_record_id)
        decision = store.upsert_record(source_id, snapshot)
        if sync_managed_contact_from_row(store, sheet_name, snapshot.normalized_json):
            counts["managed_contact_count"] = counts.get("managed_contact_count", 0) + 1
        if decision.action == "create":
            counts["created_count"] += 1
        elif decision.action == "update":
            counts["updated_count"] += 1
    if hasattr(store, "delete_missing_records"):
        deleted_count = store.delete_missing_records(source_id, seen_record_ids)
        counts["deleted_count"] = counts.get("deleted_count", 0) + int(deleted_count or 0)
    store.mark_source_synced(source_id)


def _sync_doc(
    store: Any,
    client: WeComSmartsheetClient,
    *,
    profile: str,
    docid: str,
    fallback_name: str,
    source_url: str,
    counts: dict[str, int],
    errors: list[dict[str, Any]],
    skip_unchanged: bool,
) -> None:
    """同步一个智能表格文档：实时名 + modify_time 增量跳过 + 全 sheet 发现与同步（sheet 级容错）。"""
    doc_base = client.get_doc_base(docid)
    document_name = doc_base["doc_name"] or fallback_name
    modify_time = doc_base["modify_time"]
    sheets = client.get_sheets(docid)
    sheet_titles = {_sheet_name(sheet) for sheet in sheets}
    is_control_plane = bool(sheet_titles & (set(CONTACT_SHEET_CHANNELS) | SESSION_INDEX_SHEETS))

    if skip_unchanged and modify_time and not is_control_plane:
        last_seen = store.get_doc_modified("wecom", profile, docid)
        if last_seen and last_seen == modify_time:
            counts["skipped_doc_count"] = counts.get("skipped_doc_count", 0) + 1
            print(f"[企业微信同步] {profile} 「{document_name}」modify_time 未变化（{modify_time}），整簿跳过。")
            return

    initial_error_count = int(counts.get("error_count", 0) or 0)
    seen_sheet_ids: list[str] = []
    if not sheets:
        print(f"[企业微信同步] {profile} docid={docid} 未返回 sheet。")
    for sheet in sheets:
        sheet_id = _sheet_id(sheet)
        if not sheet_id:
            continue
        seen_sheet_ids.append(sheet_id)
        sheet_name = _sheet_name(sheet)
        source_id = store.ensure_source(
            provider="wecom",
            env_profile=profile,
            source_name=compose_source_name(document_name, sheet_name),
            source_type="smartsheet_sheet",
            external_doc_id=docid,
            external_sheet_id=sheet_id,
            source_url=source_url,
            document_name=document_name,
            sheet_name=sheet_name,
        )
        # sheet 级容错：个别表报错（如公开收集表 get_records 60111）不拖垮同文档其余表。
        try:
            _sync_sheet_records(store, client, source_id, docid, sheet_id, counts, sheet_name)
        except Exception as sheet_exc:  # noqa: BLE001
            counts["error_count"] += 1
            sheet_error = str(sheet_exc)
            errors.append(
                {
                    "env_profile": profile,
                    "docid": docid,
                    "sheet_id": sheet_id,
                    "sheet_name": sheet_name,
                    "error": sheet_error,
                    "summary": summarize_wecom_error(sheet_error),
                }
            )
            print(
                f"[企业微信同步] {profile} docid={docid} "
                f"sheet={sheet_name} 同步失败（已跳过，继续其余表）：{sheet_exc}"
            )
    if (
        seen_sheet_ids
        and int(counts.get("error_count", 0) or 0) == initial_error_count
        and hasattr(store, "disable_missing_sheets")
    ):
        disabled_count = store.disable_missing_sheets("wecom", profile, docid, seen_sheet_ids)
        counts["disabled_sheet_count"] = counts.get("disabled_sheet_count", 0) + int(disabled_count or 0)
    # 全簿处理完才登记 modify_time，半途失败下轮不会被跳过。
    store.upsert_doc_source(
        provider="wecom",
        env_profile=profile,
        external_doc_id=docid,
        document_name=document_name,
        source_url=source_url,
        external_modified_at=modify_time,
    )


def run_sync_wecom_full(profiles_arg: str = "") -> int:
    profiles = env_profiles(profiles_arg)
    if not profiles:
        print("未配置 WECOM_ENV_PROFILES，也未传入 --profiles。示例：COMPANY_A,COMPANY_B")
        return 1

    store = open_store()
    exit_code = 0
    try:
        for profile in profiles:
            print(f"[企业微信同步] 开始处理公司配置：{profile}")
            run_id = store.start_run(provider="wecom", env_profile=profile, mode="full")
            counts = {
                "source_count": 0,
                "sheet_count": 0,
                "record_count": 0,
                "created_count": 0,
                "updated_count": 0,
                "error_count": 0,
            }
            errors: list[dict[str, Any]] = []
            try:
                credentials = credentials_for_profile(profile)
                registry_sources = [
                    WeComDocSource(
                        env_profile=profile,
                        docid=str(row["external_doc_id"]),
                        source_name=str(row["source_name"] or f"{profile} 登记表 docid"),
                        source_url=str(row.get("source_url") or ""),
                    )
                    for row in store.list_registry_doc_sources(provider="wecom", env_profile=profile)
                ]
                sources = list({item.docid: item for item in [*discover_profile_sources(profile), *registry_sources]}.values())
                counts["source_count"] = len(sources)
                if not credentials:
                    raise RuntimeError(f"{profile} 缺少 WECOM_{profile}_CORP_ID 或 WECOM_{profile}_APP_SECRET。")
                if not sources:
                    raise RuntimeError(f"{profile} 未配置 WEDOC_{profile}_DOCID 或 SMARTSHEET_{profile}_ID。")

                for source in sources:
                    doc_synced = False
                    last_error: Exception | None = None
                    for credential in credentials:
                        client = WeComSmartsheetClient(credential.corpid, credential.secret)
                        try:
                            _sync_doc(
                                store,
                                client,
                                profile=profile,
                                docid=source.docid,
                                fallback_name=source.source_name,
                                source_url=source.source_url,
                                counts=counts,
                                errors=errors,
                                skip_unchanged=True,
                            )
                            doc_synced = True
                            break
                        except Exception as exc:  # noqa: BLE001 - sync should keep collecting useful diagnostics.
                            last_error = exc
                            print(f"[企业微信同步] {profile} 凭证 {credential.label} 同步失败：{exc}")
                    if not doc_synced and last_error is not None:
                        counts["error_count"] += 1
                        error_text = str(last_error)
                        errors.append(
                            {
                                "env_profile": profile,
                                "docid": source.docid,
                                "error": error_text,
                                "summary": summarize_wecom_error(error_text),
                            }
                        )
                status = "success" if counts["error_count"] == 0 else "partial_failed"
            except Exception as exc:  # noqa: BLE001
                exit_code = 1
                counts["error_count"] += 1
                error_text = str(exc)
                errors.append({"env_profile": profile, "error": error_text, "summary": summarize_wecom_error(error_text)})
                status = "failed"
                print(f"[企业微信同步] {profile} 同步失败：{exc}")

            store.finish_run(run_id, status=status, counts=counts, error_json=errors)
            print(
                f"[企业微信同步] {profile} 完成：status={status} "
                f"sheets={counts['sheet_count']} records={counts['record_count']} "
                f"created={counts['created_count']} updated={counts['updated_count']} errors={counts['error_count']}"
            )
            if counts["error_count"]:
                exit_code = 1
    finally:
        store.close()

    return exit_code


def sync_wecom_source(store: Any, source_id: int, mode: str = "manual") -> tuple[str, int | None, dict[str, Any]]:
    source = store.get_source(source_id)
    if not source:
        return "failed", None, {"error": f"找不到同步源：{source_id}"}
    if source["provider"] != "wecom":
        return "failed", None, {"error": f"暂不支持该 provider：{source['provider']}"}
    if not source["external_doc_id"]:
        return "failed", None, {"error": "指定同步源缺少 docid"}
    is_doc_request = not source["external_sheet_id"]

    profile = str(source["env_profile"])
    run_id = store.start_run(provider="wecom", env_profile=profile, mode=mode)
    counts = {
        "source_count": 1,
        "sheet_count": 0,
        "record_count": 0,
        "created_count": 0,
        "updated_count": 0,
        "error_count": 0,
    }
    errors: list[dict[str, Any]] = []
    status = "failed"
    try:
        credentials = credentials_for_profile(profile)
        if not credentials:
            raise RuntimeError(f"{profile} 缺少 WECOM_{profile}_CORP_ID 或 WECOM_{profile}_APP_SECRET。")

        last_error: Exception | None = None
        for credential in credentials:
            client = WeComSmartsheetClient(credential.corpid, credential.secret)
            try:
                if is_doc_request:
                    # doc 级请求：整簿重扫（含新 sheet 发现），手动触发不做 modify_time 跳过。
                    _sync_doc(
                        store,
                        client,
                        profile=profile,
                        docid=str(source["external_doc_id"]),
                        fallback_name=str(source["source_name"] or ""),
                        source_url=str(source["source_url"] or ""),
                        counts=counts,
                        errors=errors,
                        skip_unchanged=False,
                    )
                else:
                    _sync_sheet_records(
                        store,
                        client,
                        int(source["id"]),
                        str(source["external_doc_id"]),
                        str(source["external_sheet_id"]),
                        counts,
                        str(source.get("sheet_name") or source.get("source_name") or ""),
                    )
                status = "success" if counts["error_count"] == 0 else "partial_failed"
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                print(f"[企业微信同步] source_id={source_id} 凭证 {credential.label} 同步失败：{exc}")
        if last_error is not None:
            raise last_error
    except Exception as exc:  # noqa: BLE001
        counts["error_count"] += 1
        error_text = str(exc)
        errors.append({"source_id": source_id, "error": error_text, "summary": summarize_wecom_error(error_text)})
        status = "failed"

    store.finish_run(run_id, status=status, counts=counts, error_json=errors)
    return status, run_id, {"errors": errors, "counts": counts}


def run_sync_wecom_source(source_id: int) -> int:
    store = open_store()
    try:
        status, _, detail = sync_wecom_source(store, source_id=source_id, mode="manual")
        print(f"[企业微信同步] source_id={source_id} 手动同步完成：{status} {detail}")
        return 0 if status == "success" else 1
    finally:
        store.close()


def run_pending_sync_requests(limit: int = 10) -> int:
    store = open_store()
    exit_code = 0
    try:
        requests = store.pending_sync_requests(limit=limit)
        if not requests:
            print("[企业微信同步] 当前没有待处理的手动同步请求。")
            return 0
        for request in requests:
            request_id = int(request["id"])
            source_id = int(request["source_id"])
            provider = str(request.get("provider") or "")
            print(f"[文档同步] 开始处理手动请求 request_id={request_id} source_id={source_id} provider={provider}")
            store.mark_sync_request_running(request_id)
            is_feishu = provider == "feishu"
            if is_feishu:
                status, run_id, detail = sync_feishu_source(store, source_id=source_id, mode="manual")
            else:
                status, run_id, detail = sync_wecom_source(store, source_id=source_id, mode="manual")
            # partial_failed（个别表受 API 限制）不视为请求失败。
            request_status = "success" if status in ("success", "partial_failed") else "failed"
            store.finish_sync_request(request_id, request_status, run_id, detail)
            if structure_backup_enabled() and not is_feishu:
                enqueue_copy_auto_structure_backup(store, request, request_status=request_status)
            if request_status != "success":
                exit_code = 1
    finally:
        store.close()
    return exit_code
