from __future__ import annotations

from typing import Any

from app.providers.wecom import (
    WeComDocSource,
    WeComSmartsheetClient,
    credentials_for_profile,
    discover_profile_sources,
    env_profiles,
)
from app.pipelines.managed_contacts import CONTACT_SHEET_CHANNELS, SESSION_INDEX_SHEETS, sync_managed_contact_from_row
from app.pipelines.sync_feishu_full import sync_feishu_source
from app.pipelines.document_locator import (
    record_locator_after_request,
    record_locator_failure,
    record_locator_read_success,
    reconcile_document_locators,
)
from app.storage.postgres import build_record_snapshot, compose_source_name, open_store
from app.storage.job_catalog import reconcile_document_jobs_fail_open
from app.storage.sync_job_platform import classify_error, platform_writer_for


def _platform_error(exc: Exception) -> RuntimeError:
    """Retain the fixed error kind without storing source IDs or credentials."""
    kind = classify_error(exc)
    messages = {
        "auth": "unauthorized",
        "rate_limit": "rate limit",
        "network": "network failure",
        "schema": "schema failure",
        "write": "write failure",
        "unknown": "sync failure",
    }
    return RuntimeError(messages[kind])


def _safe_error_detail(exc: Exception, **context: Any) -> dict[str, Any]:
    """Return diagnostics safe for persisted JSON and worker stdout."""
    kind = classify_error(exc)
    return {**context, "error_kind": kind, "error": str(_platform_error(exc))}


class _PlatformRun:
    """Keep platform observability isolated from the legacy sync path."""

    def __init__(self, writer: Any, run_id: int | None) -> None:
        self.writer = writer
        self.run_id = run_id
        self.current_step: tuple[int, str, int, str] | None = None
        self.outcome_counts: dict[str, int] = {}
        self.outcome_error: Exception | None = None

    @classmethod
    def start(
        cls,
        store: Any,
        *,
        source_id: int,
        source_name: str,
        legacy_run_id: int | None,
        mode: str,
    ) -> "_PlatformRun":
        if legacy_run_id is None:
            return cls(None, None)
        try:
            writer = platform_writer_for(store)
            run_id = writer.start_run(
                job_key=f"wecom.doc.{source_id}",
                kind="pull",
                provider="wecom",
                display_name=str(source_name or source_id),
                source_id=source_id,
                trigger="manual" if mode == "manual" else "schedule",
                legacy_ref={"table": "sync_runs", "id": legacy_run_id},
            )
            return cls(writer, run_id)
        except Exception:  # noqa: BLE001 - observability must not alter the legacy result.
            return cls(None, None)

    def step(self, seq: int, name: str, status: str, *, items: int = 0, message: str = "") -> None:
        if self.run_id is None or self.writer is None:
            return
        try:
            self.writer.upsert_step(self.run_id, seq, name, status, items=items, message=message)
            if status == "running":
                self.current_step = (seq, name, items, message)
            elif self.current_step and self.current_step[:2] == (seq, name):
                self.current_step = None
        except Exception:
            return

    def set_current_progress(self, items: int, message: str) -> None:
        if self.current_step is None:
            return
        seq, name, _, _ = self.current_step
        self.current_step = (seq, name, items, message)

    def fail_current(self) -> None:
        if self.current_step is None:
            return
        seq, name, items, message = self.current_step
        self.step(seq, name, "failed", items=items, message=message)

    def queue_outcome(self, counts: dict[str, int], error: Exception | None = None) -> None:
        self.outcome_counts = dict(counts)
        self.outcome_error = error

    def finish_after_legacy(self, legacy_status: str) -> None:
        if self.outcome_error:
            self.fail_current()
            self.finish(
                legacy_status="partial_failed" if legacy_status == "partial_failed" else "failed",
                counts=self.outcome_counts,
                error=self.outcome_error,
            )
            return
        self.finish(legacy_status="success", counts=self.outcome_counts)

    def finish(self, *, legacy_status: str, counts: dict[str, int], error: Exception | None = None) -> None:
        if self.run_id is None or self.writer is None:
            return
        status = "partial" if legacy_status == "partial_failed" else legacy_status
        try:
            self.writer.finish_run(
                self.run_id,
                status=status,
                row_count=int(counts.get("record_count", 0) or 0),
                changed_count=int(counts.get("created_count", 0) or 0) + int(counts.get("updated_count", 0) or 0),
                error=error,
                detail_json={
                    "error_count": int(counts.get("error_count", 0) or 0),
                    "unreadable_record_count": int(counts.get("unreadable_record_count", 0) or 0),
                },
            )
        except Exception:
            return


def _record_skipped_runs(
    store: Any, *, profile: str, docid: str, legacy_run_id: int | None, mode: str
) -> None:
    """整簿跳过也要留痕。

    跳过 ≠ 没跑，但旧写法在跳过分支直接 return，既不写 sync_job_runs 也不动
    last_sync_at，页面上「最近运行」就停在最后一次内容有变化的日子——看起来像同步坏了。
    """
    if legacy_run_id is None or not hasattr(store, "list_active_sheet_sources"):
        return
    try:
        sources = store.list_active_sheet_sources("wecom", profile, docid)
    except Exception:  # noqa: BLE001 - observability must not alter the legacy result.
        return
    for source in sources:
        try:
            source_id = int(source["source_id"])
        except (KeyError, TypeError, ValueError):
            continue
        run = _PlatformRun.start(
            store,
            source_id=source_id,
            source_name=str(source.get("source_name") or source_id),
            legacy_run_id=legacy_run_id,
            mode=mode,
        )
        run.finish(legacy_status="skipped", counts={})


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
    platform_run: _PlatformRun | None = None,
) -> None:
    if platform_run:
        platform_run.step(3, "fetch_page", "running", message=sheet_name)
    fields_response = client.get_fields(docid, sheet_id)
    field_titles = store.replace_fields(source_id, _fields_from_response(fields_response))
    records_response = client.get_records(docid, sheet_id)
    records = records_response.get("records") or []
    unreadable_count = int(records_response.get("unreadable_count") or 0)
    if platform_run:
        platform_run.step(3, "fetch_page", "success", items=len(records), message=sheet_name)
        platform_run.step(4, "normalize", "running", message=sheet_name)
    counts["sheet_count"] += 1
    counts["record_count"] += len(records)
    if unreadable_count:
        counts["unreadable_record_count"] = counts.get("unreadable_record_count", 0) + unreadable_count
    unreadable_note = (
        f"，{unreadable_count} 条企微读不出已跳过（序号 {records_response.get('unreadable_offsets') or []}）"
        if unreadable_count
        else ""
    )
    print(
        f"[企业微信同步] source_id={source_id} sheet={sheet_name or '未命名'} "
        f"完整拉取 {len(records)} 条，分页 {records_response.get('page_count', 1)} 页{unreadable_note}。"
    )
    seen_record_ids: list[str] = []
    normalized_records = []
    for record in records:
        if not isinstance(record, dict):
            continue
        snapshot = build_record_snapshot(record, field_titles)
        normalized_records.append(snapshot)
        if platform_run:
            platform_run.set_current_progress(len(normalized_records), sheet_name)

    if platform_run:
        platform_run.step(4, "normalize", "success", items=len(normalized_records), message=sheet_name)
        platform_run.step(5, "upsert", "running", message=sheet_name)

    for snapshot in normalized_records:
        seen_record_ids.append(snapshot.external_record_id)
        decision = store.upsert_record(source_id, snapshot)
        if platform_run:
            platform_run.set_current_progress(len(seen_record_ids), sheet_name)
        if sync_managed_contact_from_row(store, sheet_name, snapshot.normalized_json):
            counts["managed_contact_count"] = counts.get("managed_contact_count", 0) + 1
        if decision.action == "create":
            counts["created_count"] += 1
        elif decision.action == "update":
            counts["updated_count"] += 1
    # 本轮有记录读不出来时，"没见到"不能推断成"上游已删"——照删会把仍然存在、
    # 只是这次拉不回来的记录从库里抹掉。有不可读记录就整轮放弃删除比对。
    if unreadable_count:
        print(
            f"[企业微信同步] source_id={source_id} sheet={sheet_name or '未命名'} "
            f"存在不可读记录，本轮跳过删除比对。"
        )
    elif hasattr(store, "delete_missing_records"):
        deleted_count = store.delete_missing_records(source_id, seen_record_ids)
        counts["deleted_count"] = counts.get("deleted_count", 0) + int(deleted_count or 0)
    store.mark_source_synced(source_id)
    if platform_run:
        platform_run.step(5, "upsert", "success", items=len(normalized_records), message=sheet_name)


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
    legacy_run_id: int | None = None,
    mode: str = "full",
    platform_outcomes: list[_PlatformRun] | None = None,
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
            try:
                record_locator_read_success(store, env_profile=profile, api_doc_id=docid)
            except Exception:  # noqa: BLE001 - locator recovery must not block synchronization.
                pass
            counts["skipped_doc_count"] = counts.get("skipped_doc_count", 0) + 1
            _record_skipped_runs(
                store, profile=profile, docid=docid, legacy_run_id=legacy_run_id, mode=mode
            )
            print(f"[企业微信同步] {profile} 「{document_name}」modify_time 未变化（{modify_time}），整簿跳过。")
            return

    initial_error_count = int(counts.get("error_count", 0) or 0)
    document_failure: Exception | None = None
    seen_sheet_ids: list[str] = []
    if not sheets:
        print(f"[企业微信同步] {profile} 文档未返回 sheet。")
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
        platform_run = _PlatformRun.start(
            store,
            source_id=source_id,
            source_name=compose_source_name(document_name, sheet_name),
            legacy_run_id=legacy_run_id,
            mode=mode,
        )
        platform_run.step(1, "token", "success", items=1)
        platform_run.step(2, "list_sheets", "success", items=len(sheets))
        # sheet 级容错：个别表报错（如公开收集表 get_records 60111）不拖垮同文档其余表。
        try:
            before_counts = dict(counts)
            _sync_sheet_records(store, client, source_id, docid, sheet_id, counts, sheet_name, platform_run)
            platform_run.queue_outcome(
                {
                    "record_count": counts["record_count"] - before_counts.get("record_count", 0),
                    "created_count": counts["created_count"] - before_counts.get("created_count", 0),
                    "updated_count": counts["updated_count"] - before_counts.get("updated_count", 0),
                    "unreadable_record_count": counts.get("unreadable_record_count", 0)
                    - before_counts.get("unreadable_record_count", 0),
                    "error_count": 0,
                }
            )
            if platform_outcomes is not None:
                platform_outcomes.append(platform_run)
        except Exception as sheet_exc:  # noqa: BLE001
            document_failure = sheet_exc
            platform_run.queue_outcome({"error_count": 1}, error=_platform_error(sheet_exc))
            if platform_outcomes is not None:
                platform_outcomes.append(platform_run)
            counts["error_count"] += 1
            errors.append(_safe_error_detail(sheet_exc, env_profile=profile, sheet_name=sheet_name))
            print(
                f"[企业微信同步] {profile} sheet={sheet_name} "
                f"同步失败（已跳过，继续其余表）：{_platform_error(sheet_exc)}"
            )
    if (
        seen_sheet_ids
        and int(counts.get("error_count", 0) or 0) == initial_error_count
        and hasattr(store, "disable_missing_sheets")
    ):
        disabled_count = store.disable_missing_sheets("wecom", profile, docid, seen_sheet_ids)
        counts["disabled_sheet_count"] = counts.get("disabled_sheet_count", 0) + int(disabled_count or 0)
    # 全簿处理完才登记 modify_time，半途失败下轮不会被跳过。
    # ⚠️ 2026-08-28 修：这句注释此前只是注释——modify_time 是无条件登记的，
    # 于是 wecom.doc.2「产量统计」从 2026-08-13 失败起被每晚跳过、定时任务从未重试过。
    # 失败时把它清空，下一轮 last_seen 为空就不会命中跳过分支。
    document_source_id = store.upsert_doc_source(
        provider="wecom",
        env_profile=profile,
        external_doc_id=docid,
        document_name=document_name,
        source_url=source_url,
        external_modified_at="" if document_failure is not None else modify_time,
    )
    try:
        if document_failure is not None:
            record_locator_failure(store, source_id=document_source_id, error=document_failure)
        else:
            reconcile_document_locators(store, trigger="wecom-full", source_id=document_source_id)
    except Exception:  # noqa: BLE001 - locator metadata must not block source synchronization.
        pass


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
            platform_outcomes: list[_PlatformRun] = []
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
                                legacy_run_id=run_id,
                                mode="full",
                                platform_outcomes=platform_outcomes,
                            )
                            doc_synced = True
                            break
                        except Exception as exc:  # noqa: BLE001 - sync should keep collecting useful diagnostics.
                            last_error = exc
                            print(f"[企业微信同步] {profile} 凭证轮换同步失败：{_platform_error(exc)}")
                    if not doc_synced and last_error is not None:
                        counts["error_count"] += 1
                        errors.append(_safe_error_detail(last_error, env_profile=profile))
                        try:
                            record_locator_failure(
                                store, env_profile=profile, api_doc_id=source.docid, error=last_error,
                            )
                        except Exception:  # noqa: BLE001 - locator failure reporting is fail-open.
                            pass
                status = "success" if counts["error_count"] == 0 else "partial_failed"
            except Exception as exc:  # noqa: BLE001
                exit_code = 1
                counts["error_count"] += 1
                errors.append(_safe_error_detail(exc, env_profile=profile))
                status = "failed"
                print(f"[企业微信同步] {profile} 同步失败：{_platform_error(exc)}")

            store.finish_run(run_id, status=status, counts=counts, error_json=errors)
            for platform_run in platform_outcomes:
                platform_run.finish_after_legacy(status)
            print(
                f"[企业微信同步] {profile} 完成：status={status} "
                f"sheets={counts['sheet_count']} records={counts['record_count']} "
                f"created={counts['created_count']} updated={counts['updated_count']} errors={counts['error_count']}"
            )
            if counts["error_count"]:
                exit_code = 1
    finally:
        reconcile_document_jobs_fail_open(store)
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
    failure: Exception | None = None
    platform_outcomes: list[_PlatformRun] = []
    platform_run = None if is_doc_request else _PlatformRun.start(
        store,
        source_id=int(source["id"]),
        source_name=str(source.get("source_name") or source_id),
        legacy_run_id=run_id,
        mode=mode,
    )
    try:
        if platform_run:
            platform_run.step(1, "token", "running")
        credentials = credentials_for_profile(profile)
        if not credentials:
            raise RuntimeError(f"{profile} 缺少 WECOM_{profile}_CORP_ID 或 WECOM_{profile}_APP_SECRET。")
        if platform_run:
            platform_run.step(1, "token", "success", items=1)

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
                        legacy_run_id=run_id,
                        mode=mode,
                        platform_outcomes=platform_outcomes,
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
                        platform_run,
                    )
                status = "success" if counts["error_count"] == 0 else "partial_failed"
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                print(f"[企业微信同步] source_id={source_id} 凭证轮换同步失败：{_platform_error(exc)}")
        if last_error is not None:
            raise last_error
    except Exception as exc:  # noqa: BLE001
        failure = exc
        counts["error_count"] += 1
        errors.append(_safe_error_detail(exc, source_id=source_id))
        status = "failed"
        try:
            record_locator_failure(store, source_id=source_id, error=exc)
        except Exception:  # noqa: BLE001 - locator failure reporting is fail-open.
            pass

    store.finish_run(run_id, status=status, counts=counts, error_json=errors)
    if platform_run:
        if failure:
            platform_run.fail_current()
        platform_run.finish(
            legacy_status=status,
            counts=counts,
            error=_platform_error(failure) if failure else None,
        )
    for dynamic_platform_run in platform_outcomes:
        dynamic_platform_run.finish_after_legacy(status)
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
    requests: list[dict[str, Any]] = []
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
            try:
                record_locator_after_request(store, request, request_status)
            except Exception:  # noqa: BLE001 - locator metadata must not change request outcome.
                pass
            if request_status != "success":
                exit_code = 1
    finally:
        if requests:
            reconcile_document_jobs_fail_open(store)
        store.close()
    return exit_code
