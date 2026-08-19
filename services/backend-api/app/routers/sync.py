from __future__ import annotations

from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app import document_locator, sync_control, sync_read
from pydantic import BaseModel, Field
from app.core import _conn, require_admin
from app.routers import exports as exports_router


router = APIRouter(prefix="/v1/sync", tags=["sync-center"])
RunStatus = Literal["running", "success", "partial", "failed"]


class CopyAssetBody(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)


class RepairDocIdBody(BaseModel):
    api_doc_id: str = Field(min_length=80, max_length=256)


def _write_failure(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=500,
        detail=f"操作同步中心失败：{type(exc).__name__}",
    )


@router.get("/assets")
def sync_assets(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            return sync_control.assets(
                conn,
                tplus_items=exports_router._latest_tplus_exports(),
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"读取同步中心失败：{type(exc).__name__}",
        ) from exc


@router.get("/assets/{source_id}/download")
def sync_asset_download(
    source_id: int,
    _: dict[str, Any] = Depends(require_admin),
):
    return exports_router.exports_external_doc_download(source_id, _)


@router.get("/exports/tplus/{file_name}")
def sync_tplus_download(
    file_name: str,
    _: dict[str, Any] = Depends(require_admin),
):
    return exports_router.exports_tplus_download(file_name, _)


@router.get("/config/doc")
def sync_doc_config_get(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return sync_control.read_doc_config(_conn)


@router.put("/config/doc")
def sync_doc_config_put(
    body: sync_control.DocSyncConfigUpdate,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return sync_control.save_doc_config(
            _conn,
            body,
            str(user.get("sub") or ""),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _write_failure(exc) from exc


@router.get("/config/tplus")
def sync_tplus_config_get(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return sync_control.read_tplus_config(_conn)


@router.put("/config/tplus")
def sync_tplus_config_put(
    body: sync_control.SyncConfigUpdate,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return sync_control.save_tplus_config(
            _conn,
            body,
            str(user.get("sub") or ""),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _write_failure(exc) from exc


@router.post("/run-all")
def sync_run_all(
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            return sync_control.enqueue_all(conn, str(user.get("sub") or ""))
    except HTTPException:
        raise
    except Exception as exc:
        raise _write_failure(exc) from exc


@router.post("/assets/{source_id}/run")
def sync_asset_run(
    source_id: int,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            return sync_control.enqueue_doc_asset(
                conn,
                source_id,
                str(user.get("sub") or ""),
            )
    except sync_control.InvalidSyncTarget as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise _write_failure(exc) from exc


@router.post("/assets/{source_id}/copy")
def sync_asset_copy(
    source_id: int,
    body: CopyAssetBody,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return document_locator.copy_asset(
            _conn,
            source_id=source_id,
            idempotency_key=body.idempotency_key,
            requested_by=str(user.get("sub") or ""),
        )
    except document_locator.InvalidLocatorAction as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise _write_failure(exc) from exc


@router.put("/assets/{source_id}/docid")
def sync_asset_docid(
    source_id: int,
    body: RepairDocIdBody,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return document_locator.repair_docid(
            _conn,
            source_id=source_id,
            api_doc_id=body.api_doc_id,
            requested_by=str(user.get("sub") or ""),
        )
    except document_locator.InvalidLocatorAction as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise _write_failure(exc) from exc


@router.post("/jobs/{job_key}/run")
def sync_job_run(
    job_key: str,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            if job_key == "chanjet.full":
                return sync_control.enqueue_tplus_full(
                    conn,
                    str(user.get("sub") or ""),
                )
            return sync_control.enqueue_doc_job(
                conn,
                job_key,
                str(user.get("sub") or ""),
            )
    except sync_control.InvalidSyncTarget as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise _write_failure(exc) from exc


@router.get("/overview")
def sync_overview(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            return sync_read.overview(conn)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"读取同步中心失败：{type(exc).__name__}",
        ) from exc


@router.get("/alerts")
def sync_alerts(
    state: Literal["open", "resolved", "all"] = "open",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            return sync_read.alerts_page(
                conn,
                state=state,
                limit=limit,
                offset=offset,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"读取同步中心失败：{type(exc).__name__}",
        ) from exc


@router.get("/runs")
def sync_runs(
    job_key: str | None = None,
    provider: str | None = None,
    status: RunStatus | None = None,
    group: str | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            return sync_read.runs_page(
                conn,
                job_key=job_key,
                provider=provider,
                status=status,
                group=group,
                limit=limit,
                offset=offset,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"读取同步中心失败：{type(exc).__name__}",
        ) from exc


@router.get("/jobs/{job_key}/runs")
def sync_job_runs(
    job_key: str,
    status: RunStatus | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            if not sync_read.job_exists(conn, job_key):
                raise HTTPException(status_code=404, detail="sync job not found")
            return sync_read.runs_page(
                conn,
                job_key=job_key,
                provider=None,
                status=status,
                limit=limit,
                offset=offset,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"读取同步中心失败：{type(exc).__name__}",
        ) from exc


@router.get("/runs/{run_id}")
def sync_run_detail(
    run_id: int,
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            detail = sync_read.run_detail(conn, run_id)
            if detail is None:
                raise HTTPException(status_code=404, detail="sync run not found")
            return detail
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"读取同步中心失败：{type(exc).__name__}",
        ) from exc
