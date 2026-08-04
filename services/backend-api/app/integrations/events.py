from __future__ import annotations

import hashlib
import json
from typing import Any

from app.integrations.chanjet.schemas import ChanjetEvent


CHANJET_BOM_EVENT_TYPES = {
    "Bom_Close",
    "Bom_Open",
    "Bom_UnAudit",
    "Bom_Audit",
    "Bom_Delete",
    "Bom_Create",
    "Bom_Update",
}

PARENT_CODE_KEYS = {
    "code",
    "bomcode",
    "parentcode",
    "parentitemcode",
    "inventorycode",
    "materialcode",
}
VERSION_KEYS = {"version", "versionno", "bomversion", "versioncode"}


def stable_json_hash(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_chanjet_bom_sync_request(event: ChanjetEvent) -> dict[str, Any] | None:
    if event.msg_type not in CHANJET_BOM_EVENT_TYPES:
        return None

    parent_code = _find_first_string(event.biz_content, PARENT_CODE_KEYS)
    version = _find_first_string(event.biz_content, VERSION_KEYS)
    target_json: dict[str, Any] = {
        "event_type": event.msg_type,
        "event_id": event.event_id,
        "parent_code": parent_code,
        "version": version,
        "include_disabled": True,
        "biz_content": event.biz_content,
    }
    mode = "incremental" if parent_code or version else "full_bom"
    if mode == "full_bom":
        target_json["fallback_reason"] = "missing_bom_target"

    return {
        "provider": "chanjet",
        "module": "bom",
        "mode": mode,
        "target_json": target_json,
        "reason_event_id": event.event_id,
        "priority": 20 if mode == "incremental" else 50,
        "dedupe_key": stable_json_hash({"provider": "chanjet", "module": "bom", "mode": mode, "target": target_json}),
    }


def build_ops_attention_items(status: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    database = status.get("database") if isinstance(status.get("database"), dict) else {}
    tplus = status.get("tplus") if isinstance(status.get("tplus"), dict) else {}
    reconciliation = status.get("reconciliation") if isinstance(status.get("reconciliation"), dict) else {}
    system = status.get("system") if isinstance(status.get("system"), dict) else {}
    backups = status.get("backups") if isinstance(status.get("backups"), dict) else {}
    hosts = status.get("hosts") if isinstance(status.get("hosts"), list) else []

    if not database.get("ok", False):
        items.append({"level": "critical", "code": "database_unhealthy", "message": database.get("message", "database is not healthy")})
    if int(tplus.get("failed_requests") or 0) > 0:
        items.append({"level": "warning", "code": "tplus_failed_requests", "message": f"T+ failed requests: {tplus.get('failed_requests')}"})
    if int(tplus.get("pending_requests") or 0) >= 10:
        items.append({"level": "warning", "code": "tplus_queue_backlog", "message": f"T+ pending requests: {tplus.get('pending_requests')}"})
    if not tplus.get("last_success_at"):
        items.append({"level": "warning", "code": "tplus_no_success", "message": "T+ sync has no successful run recorded"})
    if int(reconciliation.get("needs_review") or 0) > 0:
        items.append({"level": "warning", "code": "reconciliation_needs_review", "message": f"Sync diffs need review: {reconciliation.get('needs_review')}"})
    if float(system.get("disk_percent") or 0) >= 90:
        items.append({"level": "warning", "code": "disk_high", "message": f"Disk usage is {system.get('disk_percent')}%"})
    if float(system.get("memory_percent") or 0) >= 90:
        items.append({"level": "warning", "code": "memory_high", "message": f"Memory usage is {system.get('memory_percent')}%"})
    if backups.get("status") == "failed":
        items.append({"level": "critical", "code": "backup_failed", "message": "关键备份失败，请查看备份与恢复页面"})
    elif backups.get("status") in {"warning", "unknown"}:
        items.append({"level": "warning", "code": "backup_attention", "message": "关键备份过期、未上报或副本异常"})
    for host in hosts:
        if isinstance(host, dict) and not host.get("ok", False):
            items.append({"level": "warning", "code": "host_unreachable", "message": f"{host.get('name', 'host')}: {host.get('message', 'unreachable')}"})
    chanjet_token = status.get("chanjet_token") if isinstance(status.get("chanjet_token"), dict) else {}
    if chanjet_token.get("configured") and not chanjet_token.get("ok", True):
        items.append({
            "level": "critical" if chanjet_token.get("expired") else "warning",
            "code": "chanjet_token_expiring",
            "message": f"T+ openToken {chanjet_token.get('message', '异常')}；续期链路可能已断，见 runbooks/tplus.md",
        })
    return items


def _find_first_string(value: Any, normalized_keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalize_key(str(key)) in normalized_keys and item not in (None, ""):
                return str(item)
        for item in value.values():
            found = _find_first_string(item, normalized_keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_string(item, normalized_keys)
            if found:
                return found
    return None


def _normalize_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())
