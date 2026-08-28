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
from app.storage.sync_job_platform import platform_writer_for


MIRROR_JOB_KEY = "wecom.locator_mirror"
MIRROR_JOB_DISPLAY_NAME = "企微文档定位档案镜像"
CURRENT_SHEET = "文档定位档案"
EVENT_SHEET = "定位档案变更历史"
# ⚠️「API文档ID」与「分享标识」必须分列。合成一列（原来的「文档定位ID」写的是
# api_doc_id or share_ref）会让人从镜像恢复时分不清哪个能调企微 API、哪个只是
# 人工定位用的 s3_ 分享标识——设计文档专门要求「s3_ 不能伪装成有效 docid」。
#「文档定位ID」保留继续写，只作人类速览；权威值以拆出的两列为准。
# 生产表里已存在的列不会被删（_ensure_fields 只补不删），故用新增而非改名，
# 避免留下不再更新的陈旧列。
CURRENT_FIELDS = (
    "平台",
    "企业配置",
    "文档名称",
    "文档定位ID",
    "API文档ID",
    "分享标识",
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
    "关联来源ID",
    "档案版本",
    "登记时间",
    "最后验证时间",
    "最后同步时间",
    "最后更新时间",
    "唯一键",
    "最近错误",
    "错误代码",
)
# 定位档案是文档级的；真正驱动同步的身份是表级四元组，sheet_id 与 source_id 只在这张表里。
# 没有它，光凭备份无法回答「哪个作业读哪个文档的哪张子表」。
INVENTORY_SHEET = "同步表格清单"
INVENTORY_FIELDS = (
    "平台",
    "企业配置",
    "文档名称",
    "子表名称",
    "API文档ID",
    "子表ID",
    "来源ID",
    "作业键",
    "来源类型",
    "状态",
    "最后同步时间",
    "唯一键",
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
    for index, title in enumerate((CURRENT_SHEET, EVENT_SHEET, INVENTORY_SHEET), start=1):
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
    _ensure_fields(
        client, docid, existing[INVENTORY_SHEET], INVENTORY_FIELDS, created=INVENTORY_SHEET in created_titles
    )
    return {
        "docid": docid,
        "sheets": {
            CURRENT_SHEET: existing[CURRENT_SHEET],
            EVENT_SHEET: existing[EVENT_SHEET],
            INVENTORY_SHEET: existing[INVENTORY_SHEET],
        },
    }


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
        "API文档ID": api_doc_id,
        "分享标识": share_ref,
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
        "关联来源ID": _text(locator.get("external_source_id") or ""),
        "档案版本": _text(locator.get("locator_version")),
        "登记时间": _text(locator.get("registered_at")),
        "最后验证时间": _text(locator.get("last_verified_at")),
        "最后同步时间": _text(locator.get("last_sync_at")),
        "最后更新时间": _text(locator.get("updated_at")),
        "唯一键": f"locator:{int(locator['id'])}",
        "最近错误": _text(locator.get("last_error_summary")),
        "错误代码": _text(locator.get("last_error_code")),
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


def _cells(values: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    """企微智能表格的每个单元格必须是 cell 数组。

    传裸字符串时接口照样返回 errcode=0，但值不会落库，写进去的是空行。
    """
    return {title: [{"type": "text", "text": str(text or "")}] for title, text in values.items()}


def _verify_written(
    client: Any,
    *,
    backup_docid: str,
    sheet_id: str,
    key_title: str,
    expected: dict[str, str],
) -> None:
    """写完立即回读比对，不一致就抛错。

    企微在收到裸字符串单元格时照样返回 errcode=0，值却不落库——写入侧的返回码、
    重试次数全部无效，**唯一有效判据是回读单元格文本**（见 docs/constraints/doc-sync.md）。
    此前该判据只落在测试的 fake 上，生产运行时没有，于是 2026-08-14 堆出 43 条空行、
    旧结构备份堆出 1177 条空行且全程标记成功。

    只比对期望非空的字段：空值写入后读回同样是空，无法与"没写进去"区分。
    异常信息只带字段名，不带值——docid 和分享标识不得进日志文本。
    """
    unique_key = expected.get("唯一键", "")
    row = _record_index(client, backup_docid, sheet_id, key_title).get(unique_key)
    if not row:
        raise DocumentLocatorMirrorError(f"回读校验失败：写入后按{key_title}读不到该行。")
    actual = row.get("title_values") or {}
    missing = [title for title, value in expected.items() if str(value or "") and not actual.get(title, "")]
    if missing:
        raise DocumentLocatorMirrorError(
            f"回读校验失败：{len(missing)} 个字段未落库，首个为「{missing[0]}」。"
        )
    mismatched = [
        title
        for title, value in expected.items()
        if str(value or "") and actual.get(title, "") != str(value)
    ]
    if mismatched:
        raise DocumentLocatorMirrorError(
            f"回读校验失败：{len(mismatched)} 个字段值与写入不一致，首个为「{mismatched[0]}」。"
        )


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
        client.update_records(
            backup_docid, current_sheet_id, [{"record_id": record_id, "values": _cells(current_values)}]
        )
    else:
        client.add_records(backup_docid, current_sheet_id, [{"values": _cells(current_values)}])
    _verify_written(
        client,
        backup_docid=backup_docid,
        sheet_id=current_sheet_id,
        key_title="唯一键",
        expected=current_values,
    )

    event_added = False
    event_values = _event_values(payload)
    if event_values:
        event_sheet_id = sheet_ids[EVENT_SHEET]
        event_index = _record_index(client, backup_docid, event_sheet_id, "唯一键")
        if event_values["唯一键"] not in event_index:
            client.add_records(backup_docid, event_sheet_id, [{"values": _cells(event_values)}])
            _verify_written(
                client,
                backup_docid=backup_docid,
                sheet_id=event_sheet_id,
                key_title="唯一键",
                expected=event_values,
            )
            event_added = True
    return {"current_written": True, "event_added": event_added}


def _inventory_values(source: dict[str, Any]) -> dict[str, str]:
    return {
        "平台": "企微" if source.get("provider") == "wecom" else "飞书",
        "企业配置": _text(source.get("env_profile")),
        "文档名称": _text(source.get("document_name")),
        "子表名称": _text(source.get("sheet_name")),
        "API文档ID": _text(source.get("external_doc_id")),
        "子表ID": _text(source.get("external_sheet_id")),
        "来源ID": _text(source.get("id")),
        "作业键": _text(source.get("job_key")),
        "来源类型": _text(source.get("source_type")),
        "状态": _text(source.get("status")),
        "最后同步时间": _text(source.get("last_sync_at")),
        "唯一键": f"source:{int(source['id'])}",
    }


def write_sheet_inventory(
    client: Any,
    *,
    backup_docid: str,
    sheet_ids: dict[str, str],
    sources: list[dict[str, Any]],
) -> dict[str, int]:
    """把表级同步身份整表刷进镜像。

    只写有变化的行：93 个来源每轮全量重写会白白打满写配额，也让变更历史失去意义。
    写完统一回读一次全表校验——判据同 write_locator_mirror，errcode 不作数。
    """
    inventory_sheet_id = sheet_ids[INVENTORY_SHEET]
    existing = _record_index(client, backup_docid, inventory_sheet_id, "唯一键")
    added = 0
    updated = 0
    touched: list[dict[str, str]] = []
    for source in sources:
        values = _inventory_values(source)
        current = existing.get(values["唯一键"])
        if current is None:
            client.add_records(backup_docid, inventory_sheet_id, [{"values": _cells(values)}])
            added += 1
            touched.append(values)
            continue
        stored = current.get("title_values") or {}
        if all(stored.get(title, "") == value for title, value in values.items()):
            continue
        record_id = str(current.get("record_id") or current.get("id") or "")
        if not record_id:
            raise DocumentLocatorMirrorError("同步表格清单当前行缺少 record_id。")
        client.update_records(
            backup_docid, inventory_sheet_id, [{"record_id": record_id, "values": _cells(values)}]
        )
        updated += 1
        touched.append(values)

    if touched:
        verified = _record_index(client, backup_docid, inventory_sheet_id, "唯一键")
        for values in touched:
            row = verified.get(values["唯一键"])
            if not row:
                raise DocumentLocatorMirrorError("回读校验失败：同步表格清单写入后读不到该行。")
            stored = row.get("title_values") or {}
            missing = [t for t, v in values.items() if str(v or "") and not stored.get(t, "")]
            if missing:
                raise DocumentLocatorMirrorError(
                    f"回读校验失败：同步表格清单 {len(missing)} 个字段未落库，首个为「{missing[0]}」。"
                )
    return {"added": added, "updated": updated, "total": len(sources)}


def run_sheet_inventory_mirror(*, force: bool = False) -> int:
    """刷新表级同步身份镜像。失败不得影响同步本身，只报非零退出码。"""
    if not force and not structure_backup_enabled():
        return 0
    store = open_store()
    try:
        sources = store.list_sheet_level_sources()
        if not sources:
            return 0
        client, workbook = _workbook_client()
        result = write_sheet_inventory(
            client,
            backup_docid=str(workbook["docid"]),
            sheet_ids=dict(workbook["sheets"]),
            sources=sources,
        )
        print(
            f"[同步表格清单] 共 {result['total']} 条，新增 {result['added']}，更新 {result['updated']}。"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - 镜像失败不阻断同步，下一轮全量重试。
        print(f"[同步表格清单] 刷新失败：{_redact_locator_error(str(exc))}")
        return 1
    finally:
        store.close()


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


class _MirrorPlatformRun:
    """把镜像流水线的每一轮写进 sync_job_runs。

    这条流水线走的是 document_locator_mirror_jobs 独立队列，此前从不写平台运行表，
    于是它在同步中心页面上永远显示「无记录」——登记在 sync_jobs 里只是为了让告警器
    认它。全部写入 best-effort，观测失败不得改变镜像本身的结果。
    """

    def __init__(self, writer: Any = None, run_id: int | None = None) -> None:
        self.writer = writer
        self.run_id = run_id

    @classmethod
    def start(cls, store: Any) -> "_MirrorPlatformRun":
        try:
            writer = platform_writer_for(store)
            run_id = writer.start_run(
                job_key=MIRROR_JOB_KEY,
                kind="mirror",
                provider="wecom",
                display_name=MIRROR_JOB_DISPLAY_NAME,
                source_id=None,
                trigger="event",
                legacy_ref={},
            )
        except Exception:  # noqa: BLE001 - observability must not block the mirror.
            return cls()
        return cls(writer, run_id)

    def step(self, seq: int, name: str, status: str, *, items: int = 0, message: str = "") -> None:
        if self.writer is None or self.run_id is None:
            return
        try:
            self.writer.upsert_step(self.run_id, seq, name, status, items=items, message=message)
        except Exception:  # noqa: BLE001
            return

    def finish(self, *, status: str, claimed: int, written: int, error: Exception | str | None = None) -> None:
        if self.writer is None or self.run_id is None:
            return
        try:
            self.writer.finish_run(
                self.run_id,
                status=status,
                row_count=claimed,
                changed_count=written,
                error=error,
                detail_json={"claimed_job_count": claimed, "written_job_count": written},
            )
        except Exception:  # noqa: BLE001
            return


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
        run = _MirrorPlatformRun.start(store)
        run.step(1, "claim", "success", items=len(jobs))
        written = 0
        try:
            client, workbook = _workbook_client()
        except Exception as exc:  # noqa: BLE001 - durable jobs retain the work for retry.
            safe_error = _redact_locator_error(str(exc))
            for job in jobs:
                delay = min(3600, 60 * (2 ** int(job.get("attempt_count") or 0)))
                store.retry_document_locator_mirror_job(int(job["id"]), safe_error, delay)
            run.step(2, "workbook", "failed", message=safe_error)
            run.finish(status="failed", claimed=len(jobs), written=0, error=safe_error)
            return 1
        run.step(2, "workbook", "success", items=1)
        run.step(3, "write_mirror", "running", items=0)
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
                written += 1
            except Exception as exc:  # noqa: BLE001 - one document must not block other mirror jobs.
                delay = min(3600, 60 * (2 ** int(job.get("attempt_count") or 0)))
                store.retry_document_locator_mirror_job(job_id, _redact_locator_error(str(exc)), delay)
                exit_code = 1
        run.step(3, "write_mirror", "success" if exit_code == 0 else "failed", items=written)
        run.finish(
            status="success" if exit_code == 0 else ("partial" if written else "failed"),
            claimed=len(jobs),
            written=written,
            error=None if exit_code == 0 else "mirror job failed",
        )
    finally:
        store.close()
    return exit_code
