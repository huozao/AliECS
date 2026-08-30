"""把 T+ 当前有效物料清单的父件名称核对回企微「色粉使用记录表 / 标准型号0117」，异常推飞书群。

只写「父件名称 / T+匹配状态 / T+核对时间 / T+停用」四列。
父件编码是这张表当物料清单执行标准时的主键，**失联时只标状态、绝不自动改编码**：
名称是描述性字段，改错代价小；编码判错会顺着执行链扩散。

匹配依据是 `tplus_bom_records` 中 `missing_since IS NULL` 的记录——该表按版本累积，
同一编码有多条历史记录，取错会拿到已作废的旧名称。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app import notify_client
from app.providers.wecom import WeComApiError, WeComSmartsheetClient, credentials_for_profile as wecom_credentials
from app.storage.postgres import connect
from app.storage.sync_job_platform import open_owned, safe_error_message


SOURCE_PROVIDER = "wecom"
SOURCE_PROFILE = os.getenv("TPLUS_PARENT_MATCH_WECOM_PROFILE", "COMPANY_A")
# 2026-08-11 从文档「标准型号0117」/ 表「标准型号规格&月统计」迁到这里，与 backend 的
# formula_colors.py 必须保持同一对名字。⚠️ 新文档里「标准型号011」（少个 7）和
# 「标准型号规格&月统计」（与旧表同名但缺 9 个字段）都能匹配成功却是错的源。
SOURCE_DOCUMENT = os.getenv("TPLUS_PARENT_MATCH_DOCUMENT", "色粉使用记录表")
SOURCE_SHEET = os.getenv("TPLUS_PARENT_MATCH_SHEET", "标准型号0117")

F_PARENT_CODE = "父件编码"
F_PARENT_NAME = "父件名称"
F_MATCH_STATUS = "T+匹配状态"
F_CHECKED_AT = "T+核对时间"
F_DISABLED = "T+停用"
F_MODEL = "型号"
MANAGED_FIELDS = (F_PARENT_NAME, F_MATCH_STATUS, F_CHECKED_AT, F_DISABLED)

STATUS_OK = "一致"
STATUS_RENAMED = "名称已更新"
STATUS_MISSING = "编码失联"
STATUS_NO_CODE = "无父件编码"

# 停用状态是独立的一列，不并进 T+匹配状态：停用件仍在 T+ 里，编码照样有效，
# 和「编码失联」是两回事，混一列会让人按失联去查一个其实还在的编码。
DISABLED_YES = "停用"
DISABLED_NO = "启用"

_BEIJING = timezone(timedelta(hours=8))

_ACTIVE_BOM_SQL = """
SELECT DISTINCT ON (raw_json->>'Code')
       raw_json->>'Code', raw_json->>'Name', raw_json->>'Version', raw_json->>'Disabled'
FROM tplus_bom_records
WHERE missing_since IS NULL AND coalesce(raw_json->>'Code', '') <> ''
ORDER BY raw_json->>'Code', raw_json->>'UpdateDate' DESC
"""

_ACTIVE_INVENTORY_SQL = """
SELECT raw_json->>'Code', raw_json->>'Name', raw_json->>'Disabled'
FROM tplus_inventory_records
WHERE missing_since IS NULL AND coalesce(raw_json->>'Code', '') <> ''
  AND raw_json->'InventoryClass'->>'Code' = '06'
ORDER BY raw_json->>'Code'
"""

_SOURCE_SQL = """
SELECT external_doc_id, external_sheet_id
FROM external_sources
WHERE provider = %s AND env_profile = %s AND document_name = %s AND sheet_name = %s
  AND coalesce(external_sheet_id, '') <> ''
ORDER BY id
LIMIT 1
"""


@dataclass
class MatchResult:
    checked_at: str
    total: int = 0
    with_code: int = 0
    ok: int = 0
    renamed: list[tuple[str, str, str, str]] = field(default_factory=list)
    missing: list[tuple[str, str]] = field(default_factory=list)
    disabled: list[tuple[str, str]] = field(default_factory=list)
    no_code: int = 0
    updates: list[dict[str, Any]] = field(default_factory=list)
    created_fields: list[str] = field(default_factory=list)
    created_rows: list[str] = field(default_factory=list)
    write_errors: list[str] = field(default_factory=list)
    exit_code: int = 0


def cell_text(values: dict[str, Any], key: str) -> str:
    cell = (values or {}).get(key) or []
    if isinstance(cell, str):
        return cell.strip()
    parts = [str(item.get("text") or "") for item in cell if isinstance(item, dict)]
    return "".join(parts).strip()


def text_cell(value: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": value}]


def disabled_flag(value: Any) -> bool:
    """T+ 的 Disabled 经 jsonb->>' 取出来是 'True'/'False' 文本，不是布尔。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1"}


def load_active_bom() -> dict[str, tuple[str, str, bool]]:
    """T+ 有效父件：BOM 记录优先，纯存货档案（暂无 BOM）兜底，避免新建父件被误判失联。

    第三个元素是 T+ 里的停用标记：BOM 父件取 BOM 单的 Disabled，纯存货父件取存货档案的。
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_ACTIVE_BOM_SQL)
            bom = {
                str(row[0]): (str(row[1] or ""), str(row[2] or ""), disabled_flag(row[3]))
                for row in cur.fetchall()
            }
            cur.execute(_ACTIVE_INVENTORY_SQL)
            for code, name, disabled in cur.fetchall():
                bom.setdefault(str(code), (str(name or ""), "", disabled_flag(disabled)))
            return bom


def resolve_source() -> tuple[str, str]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_SOURCE_SQL, (SOURCE_PROVIDER, SOURCE_PROFILE, SOURCE_DOCUMENT, SOURCE_SHEET))
            row = cur.fetchone()
    if not row:
        raise RuntimeError(f"未在 external_sources 找到已同步的「{SOURCE_DOCUMENT} / {SOURCE_SHEET}」")
    return str(row[0]), str(row[1])


def ensure_fields(client: WeComSmartsheetClient, docid: str, sheet_id: str) -> list[str]:
    """三列缺哪补哪；已存在的列不动，避免误改用户手工建的同名列类型。"""
    data = client.get_fields(docid, sheet_id)
    existing = {str(item.get("field_title") or "") for item in (data.get("fields") or []) if isinstance(item, dict)}
    missing = [name for name in MANAGED_FIELDS if name not in existing]
    if missing:
        client.add_fields(docid, sheet_id, [
            {"field_title": name, "field_type": "FIELD_TYPE_TEXT"} for name in missing
        ])
    return missing


def plan_updates(records: list[dict[str, Any]], bom: dict[str, tuple[str, str, bool]], checked_at: str) -> MatchResult:
    result = MatchResult(checked_at=checked_at)
    for record in records:
        values = record.get("values") or {}
        record_id = str(record.get("record_id") or "")
        if not record_id:
            continue
        result.total += 1
        code = cell_text(values, F_PARENT_CODE)
        model = cell_text(values, F_MODEL)
        current_name = cell_text(values, F_PARENT_NAME)
        current_status = cell_text(values, F_MATCH_STATUS)
        current_disabled = cell_text(values, F_DISABLED)
        # 编码没匹配上时不猜停用状态，保留原值——否则失联行会被写成「启用」，读起来像确认过。
        target_disabled = current_disabled

        if not code:
            status, target_name = STATUS_NO_CODE, current_name
            result.no_code += 1
        else:
            result.with_code += 1
            hit = bom.get(code)
            if hit is None:
                # 编码在 T+ 已不存在：只标状态，保留原名与原编码等人工确认。
                status, target_name = STATUS_MISSING, current_name
                result.missing.append((code, model))
            else:
                target_name = hit[0]
                # 停用不影响匹配判定：停用件仍在 T+ 里，编码有效，只是不该再投产。
                target_disabled = DISABLED_YES if hit[2] else DISABLED_NO
                if current_name and current_name != target_name:
                    status = STATUS_RENAMED
                    result.renamed.append((code, model, current_name, target_name))
                else:
                    status = STATUS_OK
                    result.ok += 1

        changed: dict[str, Any] = {}
        if target_name != current_name:
            changed[F_PARENT_NAME] = text_cell(target_name)
        if status != current_status:
            changed[F_MATCH_STATUS] = text_cell(status)
        if target_disabled != current_disabled:
            changed[F_DISABLED] = text_cell(target_disabled)
            # 只报「本轮新变成停用」，不报存量：存量每轮都在，报了就是每天一条同样的告警。
            if target_disabled == DISABLED_YES:
                result.disabled.append((code, model))
        # 只有真的改了才盖时间戳：补建后全表上千行，每轮重写整表既无信息量又吃接口配额。
        if changed:
            changed[F_CHECKED_AT] = text_cell(checked_at)
            result.updates.append({"record_id": record_id, "values": changed})
    return result


def plan_creates(records: list[dict[str, Any]], bom: dict[str, tuple[str, str, bool]], checked_at: str) -> list[dict[str, Any]]:
    """T+ 有、企微表没有的父件，补一行只带编码与名称的空白标准行。

    「型号」及 Lab/容差列一律留空——人工按「型号为空」筛出待补标准的行。
    编码排序是为了批次稳定，便于失败时按批重跑。
    """
    existing = {cell_text(record.get("values") or {}, F_PARENT_CODE) for record in records}
    existing.discard("")
    return [
        {"values": {
            F_PARENT_CODE: text_cell(code),
            F_PARENT_NAME: text_cell(bom[code][0]),
            F_MATCH_STATUS: text_cell(STATUS_OK),
            F_CHECKED_AT: text_cell(checked_at),
            F_DISABLED: text_cell(DISABLED_YES if bom[code][2] else DISABLED_NO),
        }}
        for code in sorted(set(bom) - existing)
    ]


def build_alert(result: MatchResult) -> str:
    lines = [
        f"【{SOURCE_DOCUMENT} · T+ 物料清单核对】",
        f"核对时间 {result.checked_at}",
        f"共 {result.total} 行，其中有父件编码 {result.with_code} 行；一致 {result.ok} 行。",
    ]
    if result.created_fields:
        lines.append("🆕 已新建列：" + "、".join(result.created_fields))
    if result.renamed:
        lines.append(f"🔄 名称已按 T+ 更新 {len(result.renamed)} 行：")
        for code, model, old, new in result.renamed[:20]:
            lines.append(f"  {code}｜{model or '-'}：{old or '(空)'} → {new}")
        if len(result.renamed) > 20:
            lines.append(f"  …另有 {len(result.renamed) - 20} 行")
    if result.missing:
        lines.append(f"⛔ 编码失联 {len(result.missing)} 行，需人工确认（未自动改编码）：")
        for code, model in result.missing[:20]:
            lines.append(f"  {code}｜{model or '-'}")
        if len(result.missing) > 20:
            lines.append(f"  …另有 {len(result.missing) - 20} 行")
    if result.disabled:
        lines.append(f"🚫 T+ 新增停用 {len(result.disabled)} 行（编码仍有效，不该再投产）：")
        for code, model in result.disabled[:20]:
            lines.append(f"  {code}｜{model or '-'}")
        if len(result.disabled) > 20:
            lines.append(f"  …另有 {len(result.disabled) - 20} 行")
    if result.created_rows:
        lines.append(f"🆕 按 T+ 补建 {len(result.created_rows)} 行（仅编码与名称，标准待人工补）：")
        for code in result.created_rows[:20]:
            lines.append(f"  {code}")
        if len(result.created_rows) > 20:
            lines.append(f"  …另有 {len(result.created_rows) - 20} 行")
    if result.write_errors:
        lines.append(f"❌ 写入失败 {len(result.write_errors)} 批：")
        for msg in result.write_errors[:10]:
            lines.append(f"  {msg}")
        if len(result.write_errors) > 10:
            lines.append(f"  …另有 {len(result.write_errors) - 10} 批")
    if not result.renamed and not result.missing and not result.disabled \
            and not result.created_rows and not result.write_errors:
        lines.append("✅ 无异常。")
    return "\n".join(lines)


def send_feishu_alert(text: str) -> bool:
    """交给统一消息中枢。收件人由 notify_routes 决定，不再读 TPLUS_PARENT_MATCH_CHAT_ID。

    推送失败仍旧不让核对本身算失败——这是收敛前的既有行为，保持不变。
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    payload = notify_client.build_payload(
        source="tplus",
        event="parent_match",
        level="warn",
        title=lines[0].strip(),
        text_segments=["\n".join(lines[1:])] if len(lines) > 1 else [],
    )
    for segment in payload["segments"]:
        if segment.get("kind") == "text":
            segment["preformatted"] = True
    return notify_client.enqueue(payload)


class _PlatformRun:
    """Best-effort platform lifecycle; it must never alter the legacy reconcile path."""

    def __init__(self, writer: Any | None, run_id: int | None, owns_writer: bool) -> None:
        self.writer = writer
        self.run_id = run_id
        self.owns_writer = owns_writer
        self.current_step: tuple[int, str, int, str] | None = None

    @classmethod
    def start(cls, *, trigger: str, writer: Any | None) -> "_PlatformRun":
        owns_writer = writer is None
        if writer is None:
            try:
                writer = open_owned()
            except Exception:  # noqa: BLE001 - observability is fail-open.
                return cls(None, None, False)
        try:
            run_id = writer.start_run(
                job_key="tplus.parent_match",
                kind="reconcile",
                provider="chanjet",
                display_name="T+ 父件核对",
                source_id=None,
                trigger=trigger,
                legacy_ref={},
            )
        except Exception:  # noqa: BLE001 - observability is fail-open.
            run_id = None
        return cls(writer, run_id, owns_writer)

    def step(self, seq: int, name: str, status: str, *, items: int = 0, message: str = "") -> None:
        if status == "running":
            self.current_step = (seq, name, items, message)
        elif self.current_step and self.current_step[:2] == (seq, name):
            self.current_step = None
        if self.writer is None or self.run_id is None:
            return
        try:
            self.writer.upsert_step(self.run_id, seq, name, status, items=items, message=message)
        except Exception:  # noqa: BLE001 - observability is fail-open.
            return

    def fail_current(self, exc: BaseException) -> None:
        if self.current_step is None:
            return
        seq, name, items, _message = self.current_step
        self.step(seq, name, "failed", items=items, message=safe_error_message(exc))

    def finish(
        self,
        *,
        status: str,
        row_count: int,
        changed_count: int,
        error: BaseException | str | None,
        detail_json: dict[str, Any],
    ) -> None:
        if self.writer is None or self.run_id is None:
            return
        try:
            self.writer.finish_run(
                self.run_id,
                status=status,
                row_count=row_count,
                changed_count=changed_count,
                error=error,
                detail_json=detail_json,
            )
        except Exception:  # noqa: BLE001 - observability is fail-open.
            return

    def close(self) -> None:
        if self.owns_writer and self.writer is not None:
            try:
                self.writer.close()
            except Exception:  # noqa: BLE001 - observability is fail-open.
                return


def _platform_detail(result: MatchResult, *, dry_run: bool, notify: bool) -> dict[str, Any]:
    return {
        "missing_count": len(result.missing),
        "renamed_count": len(result.renamed),
        "disabled_count": len(result.disabled),
        "created_field_count": len(result.created_fields),
        "created_row_count": len(result.created_rows),
        "write_error_count": len(result.write_errors),
        "dry_run": dry_run,
        "notify": notify,
    }


def run_tplus_parent_match(
    *, dry_run: bool = False, notify: bool = True, trigger: str = "manual", platform: Any | None = None
) -> int:
    platform_run = _PlatformRun.start(trigger=trigger, writer=platform)
    try:
        return _run_tplus_parent_match(dry_run=dry_run, notify=notify, platform_run=platform_run)
    except Exception as exc:
        platform_run.fail_current(exc)
        platform_run.finish(
            status="failed", row_count=0, changed_count=0, error=exc,
            detail_json={"dry_run": dry_run, "notify": notify},
        )
        raise
    finally:
        platform_run.close()


def _run_tplus_parent_match(*, dry_run: bool, notify: bool, platform_run: _PlatformRun) -> int:
    platform_run.step(1, "load_source", "running")
    creds = wecom_credentials(SOURCE_PROFILE)
    source_loaded = False
    if not creds:
        print(f"[T+核对] 企微 profile {SOURCE_PROFILE} 无凭据，跳过。")
        platform_run.step(1, "load_source", "success", message="disabled")
        platform_run.step(2, "fetch_page", "success")
        platform_run.step(3, "normalize", "success")
        platform_run.step(4, "writeback", "success", message="disabled")
        platform_run.step(5, "notify", "success", message="disabled")
        platform_run.finish(status="success", row_count=0, changed_count=0, error=None, detail_json={})
        return 0

    try:
        # 企微读侧整轮故障（corpsecret 轮换/应用权限被收回/token 失效/网络不通）最常命中这几步；
        # resolve_source() 拿不到已同步的源时也抛 RuntimeError，同一层捕获。不捕获就直接冒泡，
        # 跳过下面的 notify 分支——飞书一条告警都没有，只剩容器 stdout；事件通道还会每 poll 周期重试。
        docid, sheet_id = resolve_source()
        source_loaded = True
        platform_run.step(1, "load_source", "success", items=1)
        platform_run.step(2, "fetch_page", "running")
        client = WeComSmartsheetClient(creds[0].corpid, creds[0].secret)
        bom = load_active_bom()
        if not bom:
            print("[T+核对] tplus_bom_records 没有当前有效记录，跳过（避免把整表标成失联）。")
            platform_run.step(2, "fetch_page", "success")
            platform_run.step(3, "normalize", "success")
            platform_run.step(4, "writeback", "success", message="disabled")
            platform_run.step(5, "notify", "success", message="disabled")
            platform_run.finish(status="success", row_count=0, changed_count=0, error=None, detail_json={})
            return 0
        created = [] if dry_run else ensure_fields(client, docid, sheet_id)
        records = list(client.get_records(docid, sheet_id).get("records") or [])
    except RuntimeError as exc:
        message = safe_error_message(exc)
        if source_loaded:
            platform_run.step(2, "fetch_page", "failed", message=message)
        else:
            platform_run.step(1, "load_source", "failed", message=message)
        checked_at = datetime.now(_BEIJING).strftime("%Y-%m-%d %H:%M")
        msg = f"核对未能开始（企微读取失败，非写入问题）：{exc}"
        print(f"[T+核对] {msg}")
        if notify:
            alert_text = f"【{SOURCE_DOCUMENT} · T+ 物料清单核对】\n核对时间 {checked_at}\n❌ {msg}"
            if not send_feishu_alert(alert_text):
                print("[T+核对] 飞书告警未送达。")
        platform_run.finish(
            status="failed", row_count=0, changed_count=0, error=exc,
            detail_json={"dry_run": dry_run, "notify": notify},
        )
        return 1

    platform_run.step(2, "fetch_page", "success", items=len(records))
    platform_run.step(3, "normalize", "running")
    checked_at = datetime.now(_BEIJING).strftime("%Y-%m-%d %H:%M")
    result = plan_updates(records, bom, checked_at)
    result.created_fields = created
    creates = plan_creates(records, bom, checked_at)
    result.created_rows = [item["values"][F_PARENT_CODE][0]["text"] for item in creates]
    changed_count = len(result.updates) + len(creates)
    written_count = 0
    platform_run.step(3, "normalize", "success", items=result.total)

    print(
        f"[T+核对] 共 {result.total} 行 / 有编码 {result.with_code} / 一致 {result.ok} / "
        f"改名 {len(result.renamed)} / 失联 {len(result.missing)} / 停用 {len(result.disabled)} / "
        f"无编码 {result.no_code} / 待补建 {len(result.created_rows)}"
    )
    for code, model in result.missing:
        print(f"[T+核对] 失联 {code}｜{model}")

    if dry_run:
        for code in result.created_rows[:50]:
            print(f"[T+核对] 待补建 {code}｜{bom[code][0]}")
        print(f"[T+核对] dry-run，未写入（待补建 {len(result.created_rows)} 行）。")
        platform_run.step(4, "writeback", "success", items=changed_count, message="dry-run")
        platform_run.step(5, "notify", "success", message="dry-run")
        platform_run.finish(
            status="success", row_count=result.total, changed_count=0, error=None,
            detail_json=_platform_detail(result, dry_run=dry_run, notify=notify),
        )
        return 0

    platform_run.step(4, "writeback", "running")
    for start in range(0, len(result.updates), 200):
        batch = result.updates[start:start + 200]
        try:
            client._post("/wedoc/smartsheet/update_records", {
                "docid": docid, "sheet_id": sheet_id,
                "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE", "records": batch,
            })
            written_count += len(batch)
        except RuntimeError as exc:
            msg = f"更新第 {start // 200 + 1} 批：{exc}"
            print(f"[T+核对] {msg}")
            result.write_errors.append(msg)
            result.exit_code = 1

    for start in range(0, len(creates), 200):
        batch = creates[start:start + 200]
        try:
            client.add_records(docid, sheet_id, batch)
            written_count += len(batch)
        except RuntimeError as exc:
            msg = f"补建第 {start // 200 + 1} 批：{exc}"
            print(f"[T+核对] {msg}")
            result.write_errors.append(msg)
            result.exit_code = 1

    if result.write_errors:
        platform_run.step(4, "writeback", "failed", items=changed_count, message="write failure")
    else:
        platform_run.step(4, "writeback", "success", items=changed_count)

    platform_run.step(5, "notify", "running")
    if notify and (result.missing or result.renamed or result.disabled or result.created_fields
                   or result.created_rows or result.write_errors):
        if not send_feishu_alert(build_alert(result)):
            print("[T+核对] 飞书告警未送达。")
            result.exit_code = 1
            platform_run.step(5, "notify", "failed", message="delivery failed")
        else:
            platform_run.step(5, "notify", "success", items=1)
    elif not notify:
        platform_run.step(5, "notify", "success", message="disabled")
    else:
        platform_run.step(5, "notify", "success", message="not-needed")

    failure: BaseException | None = None
    if result.write_errors:
        failure = RuntimeError("write failure")
    elif result.exit_code:
        failure = RuntimeError("notification delivery failed")
    platform_run.finish(
        status="partial" if result.exit_code else "success",
        row_count=result.total,
        changed_count=written_count,
        error=failure,
        detail_json=_platform_detail(result, dry_run=dry_run, notify=notify),
    )
    return result.exit_code


# integration_sync_runs 有两个真实写入点，缺一都会让事件触发失灵：
#   1) tplus-sync-worker db_sync_requests.py:finish_bom_request() —— BOM builder 提交回写，
#      module 硬编码 'bom'。
#   2) tplus-sync-worker sync_state.py:record_tplus_sync_run_if_configured() —— 每日 T+ 全量，
#      由 worker_loop.py 以 module="all" 调用；这是新建物料/BOM 最常见的来源，漏掉它水位就纹丝不动。
# status = 'success' 必须过滤：module 放宽到 'all' 后，一次部分成功的全量会让 sync_state.py 的
# _mark_missing_records 把本批未出现的记录标 missing_since，此时再触发核对会把大量行误标「编码失联」
# 并发一条大告警。取值不是猜的，与 backend-api recipes.py/ops.py 的既有查询惯例一致。
_LATEST_BOM_SYNC_SQL = """
SELECT MAX(finished_at)
FROM integration_sync_runs
WHERE provider = 'chanjet' AND status = 'success' AND module IN ('all', 'bom')
"""


def latest_bom_sync_at() -> datetime | None:
    """T+ BOM 最近一次同步的完成时间；读不到一律返回 None（不抛，不拖垮轮询）。"""
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_LATEST_BOM_SYNC_SQL)
                row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception as exc:  # noqa: BLE001 - 水位读不到只是本轮不触发
        print(f"[T+核对] 读取 BOM 同步水位失败：{exc}")
        return None


def run_backfill_if_bom_synced(last_seen: datetime | None) -> tuple[datetime | None, bool]:
    """BOM 同步水位涨了就跑一次核对+补建，返回 (新水位, 是否真的跑了)。

    首轮（last_seen 为 None）只记水位不跑：容器重启风暴不该反复触发，当天的兜底轮已覆盖。
    读不到水位时保持原值——清成 None 会让下一轮把首轮逻辑再走一遍，白跑一次。
    """
    current = latest_bom_sync_at()
    if current is None:
        return last_seen, False
    if last_seen is None or current <= last_seen:
        return current, False
    exit_code = run_tplus_parent_match(trigger="event")
    if exit_code != 0:
        # 水位仍然推进，不重试：失败可能是永久性的（比如某行数据被企微一直拒绝），
        # 不推进会变成每 30s 一次飞书告警风暴。失败已经在 run_tplus_parent_match 里推过告警，
        # 这里只把非零码留痕到 stdout，当天兜底轮会再跑一次。
        print(f"[T+核对] 事件触发的核对未完全成功（exit_code={exit_code}），水位仍推进，等下次 BOM 同步再触发。")
    return current, True
