from __future__ import annotations

from typing import Any


def reconcile_document_jobs_fail_open(store: Any) -> dict[str, int] | None:
    """Refresh the document job catalog without affecting the legacy sync path."""
    try:
        writer = getattr(store, "sync_jobs", None)
        reconcile = getattr(writer, "reconcile_document_jobs", None)
        if callable(reconcile):
            return reconcile()
    except Exception:  # noqa: BLE001 - catalog observability must remain fail-open
        return None
    return None
