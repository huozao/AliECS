from __future__ import annotations

from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app import sync_read
from app.core import _conn, require_admin


router = APIRouter(prefix="/v1/sync", tags=["sync-center"])
RunStatus = Literal["running", "success", "partial", "failed"]


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
