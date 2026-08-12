from __future__ import annotations

from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app import sync_read
from app.core import _conn, require_admin


router = APIRouter(prefix="/v1/sync", tags=["sync-center"])


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
