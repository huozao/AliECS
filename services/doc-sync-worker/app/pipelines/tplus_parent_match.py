"""把 T+ 当前有效物料清单的父件名称核对回企微「标准型号0117」，异常推飞书群。

只写「父件名称 / T+匹配状态 / T+核对时间」三列。
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

from app.providers.feishu import FeishuBitableClient, credentials_for_profile as feishu_credentials
from app.providers.wecom import WeComSmartsheetClient, credentials_for_profile as wecom_credentials
from app.storage.postgres import connect


SOURCE_PROVIDER = "wecom"
SOURCE_PROFILE = os.getenv("TPLUS_PARENT_MATCH_WECOM_PROFILE", "COMPANY_A")
SOURCE_DOCUMENT = os.getenv("TPLUS_PARENT_MATCH_DOCUMENT", "标准型号0117")
SOURCE_SHEET = os.getenv("TPLUS_PARENT_MATCH_SHEET", "标准型号规格&月统计")

F_PARENT_CODE = "父件编码"
F_PARENT_NAME = "父件名称"
F_MATCH_STATUS = "T+匹配状态"
F_CHECKED_AT = "T+核对时间"
F_MODEL = "型号"
MANAGED_FIELDS = (F_PARENT_NAME, F_MATCH_STATUS, F_CHECKED_AT)

STATUS_OK = "一致"
STATUS_RENAMED = "名称已更新"
STATUS_MISSING = "编码失联"
STATUS_NO_CODE = "无父件编码"

_BEIJING = timezone(timedelta(hours=8))

_ACTIVE_BOM_SQL = """
SELECT DISTINCT ON (raw_json->>'Code')
       raw_json->>'Code', raw_json->>'Name', raw_json->>'Version'
FROM tplus_bom_records
WHERE missing_since IS NULL AND coalesce(raw_json->>'Code', '') <> ''
ORDER BY raw_json->>'Code', raw_json->>'UpdateDate' DESC
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
    no_code: int = 0
    updates: list[dict[str, Any]] = field(default_factory=list)
    created_fields: list[str] = field(default_factory=list)
    exit_code: int = 0


def cell_text(values: dict[str, Any], key: str) -> str:
    cell = (values or {}).get(key) or []
    if isinstance(cell, str):
        return cell.strip()
    parts = [str(item.get("text") or "") for item in cell if isinstance(item, dict)]
    return "".join(parts).strip()


def text_cell(value: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": value}]


def load_active_bom() -> dict[str, tuple[str, str]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_ACTIVE_BOM_SQL)
            return {str(row[0]): (str(row[1] or ""), str(row[2] or "")) for row in cur.fetchall()}


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


def plan_updates(records: list[dict[str, Any]], bom: dict[str, tuple[str, str]], checked_at: str) -> MatchResult:
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
        # 核对时间每轮都刷新，方便一眼看出数据新鲜度。
        changed[F_CHECKED_AT] = text_cell(checked_at)
        result.updates.append({"record_id": record_id, "values": changed})
    return result


def plan_creates(records: list[dict[str, Any]], bom: dict[str, tuple[str, str]], checked_at: str) -> list[dict[str, Any]]:
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
    if not result.renamed and not result.missing:
        lines.append("✅ 无异常。")
    return "\n".join(lines)


def send_feishu_alert(text: str) -> bool:
    chat_id = os.getenv("TPLUS_PARENT_MATCH_CHAT_ID", "").strip()
    if not chat_id:
        print("[T+核对] 未配置 TPLUS_PARENT_MATCH_CHAT_ID，跳过飞书推送。")
        return False
    profile = os.getenv("TPLUS_PARENT_MATCH_FEISHU_PROFILE", "COMPANY_A")
    creds = feishu_credentials(profile)
    if not creds:
        print(f"[T+核对] 飞书 profile {profile} 无凭据，跳过推送。")
        return False
    client = FeishuBitableClient(creds[0].app_id, creds[0].app_secret, creds[0].api_base)
    try:
        client._request_json(
            "POST", "/im/v1/messages",
            headers=client._headers(),
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 推送失败不应让核对本身算失败
        print(f"[T+核对] 飞书推送失败：{exc}")
        return False


def run_tplus_parent_match(*, dry_run: bool = False, notify: bool = True) -> int:
    creds = wecom_credentials(SOURCE_PROFILE)
    if not creds:
        print(f"[T+核对] 企微 profile {SOURCE_PROFILE} 无凭据，跳过。")
        return 0
    docid, sheet_id = resolve_source()
    client = WeComSmartsheetClient(creds[0].corpid, creds[0].secret)
    bom = load_active_bom()
    if not bom:
        print("[T+核对] tplus_bom_records 没有当前有效记录，跳过（避免把整表标成失联）。")
        return 0

    created = [] if dry_run else ensure_fields(client, docid, sheet_id)
    records = list(client.get_records(docid, sheet_id).get("records") or [])
    checked_at = datetime.now(_BEIJING).strftime("%Y-%m-%d %H:%M")
    result = plan_updates(records, bom, checked_at)
    result.created_fields = created

    print(
        f"[T+核对] 共 {result.total} 行 / 有编码 {result.with_code} / 一致 {result.ok} / "
        f"改名 {len(result.renamed)} / 失联 {len(result.missing)} / 无编码 {result.no_code}"
    )
    for code, model in result.missing:
        print(f"[T+核对] 失联 {code}｜{model}")

    if dry_run:
        print("[T+核对] dry-run，未写入。")
        return 0

    for start in range(0, len(result.updates), 200):
        batch = result.updates[start:start + 200]
        response = client._post("/wedoc/smartsheet/update_records", {
            "docid": docid, "sheet_id": sheet_id,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE", "records": batch,
        })
        if response.get("errcode") not in (0, None):
            print(f"[T+核对] 写入失败 errcode={response.get('errcode')} errmsg={response.get('errmsg')}")
            result.exit_code = 1

    if notify and (result.missing or result.renamed or result.created_fields):
        send_feishu_alert(build_alert(result))
    return result.exit_code
