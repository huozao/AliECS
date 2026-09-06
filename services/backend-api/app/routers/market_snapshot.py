"""黄金跨市场看板只读接口：读取采集器原子发布的最新聚合快照。"""

from __future__ import annotations

import hmac
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.core import require_login


router = APIRouter()

_ROW_FIELDS = {
    "au_symbol",
    "source_status",
    "source_timestamp",
    "ingested_at",
    "au_price_cny_per_g",
    "international_cny_per_g",
    "spread_cny_per_g",
    "xauusd_usd_per_oz",
    "usdcnh",
    "comparison_status",
    "fallback_reason",
}
_COMPARISON_FIELDS = {
    "bucket_seconds",
    "compared_buckets",
    "mismatch_buckets",
    "mt5_only_buckets",
    "dukascopy_only_buckets",
}
_MAX_INGEST_BYTES = 2 * 1024 * 1024


def _snapshot_path() -> Path:
    """快照路径只来自服务端环境变量，浏览器不能通过参数选择文件。"""

    return Path(os.getenv("MARKET_SNAPSHOT_FILE", "/app/market-data/latest.json"))


def _read_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="market snapshot is temporarily unavailable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise HTTPException(status_code=503, detail="market snapshot schema is invalid")
    return payload


def _public_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {key: row[key] for key in _ROW_FIELDS if key in row}


def _empty_snapshot() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "empty",
        "message": "暂无快照。采集器尚未发布数据。",
        "source_timestamp": None,
        "ingested_at": None,
        "contract_count": 0,
        "rows": [],
        "comparison": {
            "bucket_seconds": 1,
            "compared_buckets": 0,
            "mismatch_buckets": 0,
            "mt5_only_buckets": 0,
            "dukascopy_only_buckets": 0,
        },
    }


def _write_snapshot_atomic(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _ingest_token() -> str:
    return os.getenv("MARKET_SNAPSHOT_INGEST_TOKEN", "").strip()


def _normalize_ingest_payload(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or body.get("schema_version") != 1:
        raise HTTPException(status_code=422, detail="market snapshot schema is invalid")
    raw_rows = body.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > 2000:
        raise HTTPException(status_code=422, detail="market snapshot rows are invalid")
    rows = [_public_row(row) for row in raw_rows if isinstance(row, dict) and row.get("au_symbol")]
    comparison = body.get("comparison")
    if not isinstance(comparison, dict):
        comparison = _empty_snapshot()["comparison"]
    return {
        "schema_version": 1,
        "status": body.get("status", "ok" if rows else "empty"),
        "message": body.get("message", ""),
        "warning": body.get("warning", ""),
        "source_timestamp": body.get("source_timestamp"),
        "ingested_at": body.get("ingested_at"),
        "contract_count": len(rows),
        "rows": rows,
        "comparison": {key: comparison[key] for key in _COMPARISON_FIELDS if key in comparison},
    }


@router.get("/v1/market/snapshot")
def market_snapshot(
    limit: int = Query(default=200, ge=1, le=2000),
    _: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    payload = _read_snapshot(_snapshot_path()) or _empty_snapshot()
    raw_rows = payload.get("rows")
    rows = [_public_row(row) for row in raw_rows] if isinstance(raw_rows, list) else []
    rows = [row for row in rows if row.get("au_symbol")][:limit]
    comparison = payload.get("comparison")
    if not isinstance(comparison, dict):
        comparison = _empty_snapshot()["comparison"]
    public_comparison = {key: comparison[key] for key in _COMPARISON_FIELDS if key in comparison}
    result = {
        "schema_version": 1,
        "status": payload.get("status", "ok" if rows else "empty"),
        "message": payload.get("message", ""),
        "warning": payload.get("warning", ""),
        "source_timestamp": payload.get("source_timestamp"),
        "ingested_at": payload.get("ingested_at"),
        "contract_count": len(rows),
        "rows": rows,
        "comparison": public_comparison,
    }
    return result


@router.post("/v1/internal/market/snapshot")
async def ingest_market_snapshot(
    request: Request,
    body: dict[str, Any],
    x_market_snapshot_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """接收采集器发布的聚合快照；不接受浏览器用户令牌。"""

    expected = _ingest_token()
    supplied = (x_market_snapshot_token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="market snapshot ingest is disabled")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid market snapshot ingest token")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_INGEST_BYTES:
        raise HTTPException(status_code=413, detail="market snapshot payload is too large")
    payload = _normalize_ingest_payload(body)
    _write_snapshot_atomic(payload, _snapshot_path())
    return {"ok": True, "contract_count": payload["contract_count"], "ingested_at": payload["ingested_at"]}
