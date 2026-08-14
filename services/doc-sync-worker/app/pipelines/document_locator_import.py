from __future__ import annotations

import json
import re
import sys
from typing import Any

from app.storage.postgres import open_store


_SHARE_REF_RE = re.compile(r"s3_[A-Za-z0-9_-]+")


def valid_wecom_docid(value: str) -> bool:
    text = str(value or "")
    return text.startswith("dc") and len(text) >= 80


def share_ref_from_url(value: str) -> str:
    match = _SHARE_REF_RE.search(str(value or ""))
    return match.group(0) if match else ""


def _registry_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    registries = payload.get("registries")
    if isinstance(registries, list):
        return [item for item in registries if isinstance(item, dict)]
    return [payload]


def registry_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for registry in _registry_payloads(payload):
        docs = registry.get("docs") if isinstance(registry.get("docs"), dict) else {}
        for key, raw_item in docs.items():
            item = raw_item if isinstance(raw_item, dict) else {}
            raw_docid = str(item.get("docid") or key or "").strip()
            api_doc_id = raw_docid if valid_wecom_docid(raw_docid) else ""
            share_ref = share_ref_from_url(str(item.get("url") or ""))
            if not share_ref and raw_docid.startswith("s3_"):
                share_ref = raw_docid
            identity = ("api", api_doc_id) if api_doc_id else ("share", share_ref)
            if not identity[1] or identity in seen:
                continue
            seen.add(identity)
            admin_userid = str(item.get("admin_userid") or "").strip()
            entries.append(
                {
                    "api_doc_id": api_doc_id,
                    "share_ref": share_ref,
                    "document_name": str(item.get("doc_name") or "").strip(),
                    "source_url": str(item.get("url") or "").strip(),
                    "admin_userids": [admin_userid] if admin_userid else [],
                    "env_profile": str(item.get("env_profile") or "").strip(),
                    "sheet_count": len(item.get("sheets") or {}) if isinstance(item.get("sheets"), dict) else 0,
                }
            )
    return entries


def import_document_locators(payload: dict[str, Any], store: Any) -> dict[str, int]:
    counts = {"inserted": 0, "updated": 0, "linked": 0, "unresolved": 0, "conflicts": 0}
    for entry in registry_entries(payload):
        sources = store.find_document_locator_sources(
            api_doc_id=entry["api_doc_id"],
            share_ref=entry["share_ref"] if not entry["api_doc_id"] else "",
        )
        profiles = {str(source.get("env_profile") or "") for source in sources if source.get("env_profile")}
        explicit_profile = str(entry.get("env_profile") or "")
        if len(profiles) > 1 or (explicit_profile and profiles and explicit_profile not in profiles):
            counts["conflicts"] += 1
            continue
        env_profile = explicit_profile or (next(iter(profiles)) if profiles else "")
        if not env_profile:
            counts["conflicts"] += 1
            continue
        source = sources[0] if len(sources) == 1 else {}
        resolved = bool(entry["api_doc_id"])
        live_name = str(source.get("document_name") or "").strip()
        last_sync_at = source.get("last_sync_at")
        locator = {
            "provider": "wecom",
            "env_profile": env_profile,
            "api_doc_id": entry["api_doc_id"] or None,
            "share_ref": entry["share_ref"] or None,
            "document_name": live_name or entry["document_name"],
            "source_url": str(source.get("source_url") or entry["source_url"] or ""),
            "admin_userids": entry["admin_userids"],
            "credential_ref": env_profile,
            "source_kind": "registry",
            "lifecycle_status": "active" if resolved else "unresolved",
            "syncability_status": "verified" if resolved and last_sync_at else ("unverified" if resolved else "invalid-id"),
            "capabilities": {
                "read": "verified" if resolved and last_sync_at else ("unverified" if resolved else "unavailable"),
                "write": "unknown",
                "copy": "unverified" if resolved else "unavailable",
            },
            "sheet_count": int(source.get("sheet_count") or entry["sheet_count"] or 0),
            "external_source_id": source.get("id"),
            "last_verified_at": last_sync_at if resolved and last_sync_at else None,
            "last_sync_at": last_sync_at,
            "last_error_code": "" if resolved else "invalid-docid",
            "last_error_summary": "" if resolved else "缺少有效企微 docid",
        }
        result = store.upsert_document_locator(locator, event_type="registry-import", actor="registry-importer")
        counts["inserted" if result.get("created") else "updated"] += 1
        if resolved and source:
            counts["linked"] += 1
        if not resolved:
            counts["unresolved"] += 1
    return counts


def run_import_document_locators_from_stdin() -> int:
    payload = json.load(sys.stdin)
    store = open_store()
    try:
        result = import_document_locators(payload, store)
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["conflicts"] == 0 else 1
