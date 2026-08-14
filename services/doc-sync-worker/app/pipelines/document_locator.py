from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.storage.sync_job_platform import classify_error


_SHARE_REF_RE = re.compile(r"s3_[A-Za-z0-9_-]+")


def _valid_wecom_docid(value: str) -> bool:
    return value.startswith("dc") and len(value) >= 80


def _share_ref(source: dict[str, Any]) -> str:
    external = str(source.get("external_doc_id") or "")
    if external.startswith("s3_"):
        return external
    match = _SHARE_REF_RE.search(str(source.get("source_url") or ""))
    return match.group(0) if match else ""


def locator_from_source(source: dict[str, Any]) -> dict[str, Any]:
    provider = str(source.get("provider") or "")
    external_doc_id = str(source.get("external_doc_id") or "")
    resolved = bool(external_doc_id) if provider == "feishu" else _valid_wecom_docid(external_doc_id)
    active = str(source.get("status") or "") == "active"
    last_sync_at = source.get("last_sync_at")
    source_type = str(source.get("source_type") or "")
    system_managed = source_type == "structure_backup_doc"
    current_error = str(source.get("locator_last_error_code") or "")
    locator_updated_at = source.get("locator_updated_at")
    failure_is_newer = bool(
        current_error
        and locator_updated_at
        and (not last_sync_at or str(locator_updated_at) >= str(last_sync_at))
    )
    if failure_is_newer:
        syncability = str(source.get("locator_syncability_status") or "unverified")
    elif resolved and active and last_sync_at:
        syncability = "verified"
    elif resolved:
        syncability = "unverified"
    else:
        syncability = "invalid-id"
    read_capability = "verified" if resolved and last_sync_at else ("unverified" if resolved else "unavailable")
    copy_capability = "allowed" if provider == "wecom" and resolved and not system_managed else "unavailable"
    if failure_is_newer:
        read_capability = "unavailable"
        copy_capability = "unavailable"
    return {
        "provider": provider,
        "env_profile": str(source.get("env_profile") or ""),
        "api_doc_id": external_doc_id if resolved else None,
        "share_ref": _share_ref(source) or None,
        "document_name": str(source.get("document_name") or ""),
        "source_url": str(source.get("source_url") or ""),
        "source_kind": source_type or "discovered",
        "lifecycle_status": "unresolved" if not resolved else ("active" if active else "disabled"),
        "syncability_status": syncability,
        "capabilities": {
            "read": read_capability,
            "write": "verified" if system_managed and last_sync_at else "unknown",
            "copy": copy_capability,
        },
        "sheet_count": int(source.get("sheet_count") or 0),
        "external_source_id": int(source["id"]),
        "last_verified_at": last_sync_at if resolved and last_sync_at else None,
        "last_sync_at": last_sync_at,
        "last_error_code": current_error if failure_is_newer else ("" if resolved else "invalid-docid"),
        "last_error_summary": (
            str(source.get("locator_last_error_summary") or "同步验证失败")
            if failure_is_newer else ("" if resolved else "缺少有效企微 docid")
        ),
    }


def record_locator_failure(
    store: Any,
    *,
    error: BaseException,
    source_id: int | None = None,
    env_profile: str = "",
    api_doc_id: str = "",
) -> bool:
    kind = classify_error(error)
    message = str(error).lower()
    invalid_id = "301085" in message or "invalid docid" in message
    permission_denied = kind == "auth" or any(
        marker in message for marker in ("permission", "forbidden", "not allow", "60020")
    )
    syncability = "invalid-id" if invalid_id else ("permission-denied" if permission_denied else "unverified")
    error_code = "invalid-docid" if invalid_id else ("auth" if permission_denied else kind)
    summary = "企微文档标识失效" if invalid_id else ("企微权限验证失败" if permission_denied else "企微同步验证失败")
    written = False
    for source in store.list_document_locator_sources(source_id=source_id):
        if env_profile and str(source.get("env_profile") or "") != env_profile:
            continue
        if api_doc_id and str(source.get("external_doc_id") or "") != api_doc_id:
            continue
        locator = locator_from_source(source)
        locator.update(
            {
                "syncability_status": syncability,
                "capabilities": {"read": "unavailable", "write": "unknown", "copy": "unavailable"},
                "last_verified_at": None,
                "last_error_code": error_code,
                "last_error_summary": summary,
            }
        )
        store.upsert_document_locator(locator, event_type="sync-failed", actor="doc-sync-worker")
        written = True
    return written


def record_locator_read_success(
    store: Any,
    *,
    source_id: int | None = None,
    env_profile: str = "",
    api_doc_id: str = "",
) -> bool:
    """Clear a stale permission failure after a live document read succeeds."""
    written = False
    verified_at = datetime.now(tz=timezone.utc)
    for source in store.list_document_locator_sources(source_id=source_id):
        if env_profile and str(source.get("env_profile") or "") != env_profile:
            continue
        if api_doc_id and str(source.get("external_doc_id") or "") != api_doc_id:
            continue
        locator = locator_from_source(source)
        system_managed = str(source.get("source_type") or "") == "structure_backup_doc"
        locator.update(
            {
                "syncability_status": "verified",
                "capabilities": {
                    "read": "verified",
                    "write": "verified" if system_managed else "unknown",
                    "copy": "allowed" if not system_managed else "unavailable",
                },
                "last_verified_at": verified_at,
                "last_error_code": "",
                "last_error_summary": "",
            }
        )
        store.upsert_document_locator(locator, event_type="permission-restored", actor="doc-sync-worker")
        written = True
    return written


def reconcile_document_locators(
    store: Any,
    *,
    trigger: str,
    source_id: int | None = None,
) -> dict[str, int]:
    sources = store.list_document_locator_sources(source_id=source_id)
    result = {"seen": len(sources), "changed": 0, "failed": 0}
    for source in sources:
        try:
            written = store.upsert_document_locator(
                locator_from_source(source),
                event_type=str(trigger),
                actor="doc-sync-worker",
            )
            if written.get("changed"):
                result["changed"] += 1
        except Exception:  # noqa: BLE001 - locator metadata must not block source synchronization.
            result["failed"] += 1
    return result


def record_locator_after_request(
    store: Any,
    request: dict[str, Any],
    request_status: str,
) -> bool:
    if request_status != "success":
        return False
    request_id = int(request["id"])
    source_id = int(request["source_id"])
    result = reconcile_document_locators(
        store,
        trigger=f"sync-request:{request_id}",
        source_id=source_id,
    )
    return result["seen"] > 0 and result["failed"] == 0
