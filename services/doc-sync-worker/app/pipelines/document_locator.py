from __future__ import annotations

import re
from typing import Any


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
    if resolved and active and last_sync_at:
        syncability = "verified"
    elif resolved:
        syncability = "unverified"
    else:
        syncability = "invalid-id"
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
            "read": "verified" if resolved and last_sync_at else ("unverified" if resolved else "unavailable"),
            "write": "verified" if system_managed and last_sync_at else "unknown",
            "copy": "unverified" if provider == "wecom" and resolved and not system_managed else "unavailable",
        },
        "sheet_count": int(source.get("sheet_count") or 0),
        "external_source_id": int(source["id"]),
        "last_verified_at": last_sync_at if resolved and last_sync_at else None,
        "last_sync_at": last_sync_at,
        "last_error_code": "" if resolved else "invalid-docid",
        "last_error_summary": "" if resolved else "缺少有效企微 docid",
    }


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
