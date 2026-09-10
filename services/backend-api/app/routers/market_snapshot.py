"""黄金跨市场看板只读接口：读取采集器原子发布的最新聚合快照。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hmac
import json
import logging
import os
import tempfile
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import psycopg
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
    "available",
    "bucket_seconds",
    "compared_buckets",
    "mismatch_buckets",
    "mt5_only_buckets",
    "dukascopy_only_buckets",
}
_MAX_INGEST_BYTES = 2 * 1024 * 1024
_MARKET_INGEST_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-ingest")
# The permit covers the full synchronous transaction, not merely queueing it.
# Keeping this separate from Starlette's default worker pool leaves that pool
# available for the synchronous OIDC callback/login routes.
_MARKET_INGEST_CAPACITY = threading.BoundedSemaphore(value=1)
_ingest_logger = logging.getLogger("aliecs.market_ingest")


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
            "available": False,
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


def _run_market_ingest(work: Any, kind: str) -> dict[str, Any]:
    """Run one market write and release capacity only after it has completed."""

    started = time.monotonic()
    try:
        result = work()
        _ingest_logger.info("market ingest committed kind=%s elapsed_ms=%d", kind,
                            round((time.monotonic() - started) * 1000))
        return result
    except Exception:
        _ingest_logger.exception("market ingest failed kind=%s elapsed_ms=%d", kind,
                                 round((time.monotonic() - started) * 1000))
        raise
    finally:
        _MARKET_INGEST_CAPACITY.release()


async def _submit_market_ingest(work: Any, kind: str) -> dict[str, Any]:
    """Bound a synchronous market write without blocking the ASGI event loop.

    A disconnected client may cancel its await, but the started operation retains
    its permit until its transaction commits or rolls back. Publishers then retry
    the existing idempotent payload rather than racing another write.
    """

    if not _MARKET_INGEST_CAPACITY.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="market ingest is busy; retry shortly",
                            headers={"Retry-After": "1"})
    try:
        future = asyncio.get_running_loop().run_in_executor(
            _MARKET_INGEST_EXECUTOR, _run_market_ingest, work, kind,
        )
    except Exception:
        _MARKET_INGEST_CAPACITY.release()
        raise
    try:
        try:
            return await asyncio.shield(future)
        except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled) as exc:
            raise HTTPException(status_code=503,
                                detail="market ingest timed out; retry shortly",
                                headers={"Retry-After": "1"}) from exc
    except asyncio.CancelledError:
        # Do not release the permit here: the executor finally block owns it.
        _ingest_logger.info("market ingest client cancelled kind=%s; transaction continues", kind)
        raise


async def _read_ingest_wire(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_INGEST_BYTES:
        raise HTTPException(status_code=413, detail="market snapshot payload is too large")
    wire = bytearray()
    async for chunk in request.stream():
        if len(wire) + len(chunk) > _MAX_INGEST_BYTES:
            raise HTTPException(status_code=413, detail="market snapshot payload is too large")
        wire.extend(chunk)
    return bytes(wire)


def _gunzip_ingest_wire(wire: bytes) -> bytes:
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = bytearray()
    pending = wire
    try:
        while pending:
            chunk = inflater.decompress(pending, _MAX_INGEST_BYTES + 1 - len(output))
            output.extend(chunk)
            if len(output) > _MAX_INGEST_BYTES:
                raise HTTPException(status_code=413, detail="market snapshot payload is too large")
            if inflater.unused_data:
                raise HTTPException(status_code=400, detail="market snapshot gzip has trailing data")
            pending = inflater.unconsumed_tail
        if not inflater.eof:
            raise HTTPException(status_code=400, detail="market snapshot gzip is malformed or truncated")
        output.extend(inflater.flush(_MAX_INGEST_BYTES + 1 - len(output)))
    except zlib.error as exc:
        raise HTTPException(status_code=400, detail="market snapshot gzip is malformed or truncated") from exc
    if len(output) > _MAX_INGEST_BYTES:
        raise HTTPException(status_code=413, detail="market snapshot payload is too large")
    if inflater.unused_data:
        raise HTTPException(status_code=400, detail="market snapshot gzip has trailing data")
    return bytes(output)


async def _decode_ingest_body(request: Request) -> dict[str, Any]:
    wire = await _read_ingest_wire(request)
    encoding = request.headers.get("content-encoding", "identity").strip().lower() or "identity"
    if encoding == "gzip":
        raw = _gunzip_ingest_wire(wire)
    elif encoding == "identity":
        raw = wire
    else:
        raise HTTPException(status_code=415, detail="unsupported market snapshot content encoding")
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="market snapshot body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="market snapshot body must be a JSON object")
    return body


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
    x_market_snapshot_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """接收采集器发布的聚合快照；不接受浏览器用户令牌。"""

    expected = _ingest_token()
    supplied = (x_market_snapshot_token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="market snapshot ingest is disabled")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid market snapshot ingest token")
    body = await _decode_ingest_body(request)
    if body.get("schema_version") == market_review.SCHEMA:
        return await _submit_market_ingest(lambda: market_review.ingest(body), "review")
    payload = _normalize_ingest_payload(body)
    path = _snapshot_path()

    def write_legacy_snapshot() -> dict[str, Any]:
        _write_snapshot_atomic(payload, path)
        return {"ok": True, "contract_count": payload["contract_count"],
                "ingested_at": payload["ingested_at"]}

    return await _submit_market_ingest(write_legacy_snapshot, "snapshot")

# Versioned review endpoints keep legacy /snapshot untouched.
from app import market_review
from app.core import require_permission


def _review_reader(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission('market.read', user)
    # Login tokens carry uid/sub; retain compatibility with legacy id/username
    # sessions. Only server-authenticated identity may own reads/annotations.
    identity = user.get('uid', user.get('id'))
    if type(identity) is not int or identity <= 0:
        raise HTTPException(401, 'invalid authenticated user identity')
    return dict(user, id=identity, username=user.get('username') or user.get('sub'))


@router.get('/v1/market/latest')
def market_latest(_: dict = Depends(_review_reader)):
    return market_review.latest() or _empty_snapshot()


@router.get('/v1/market/realtime')
def market_realtime(after: str | None = Query(default=None, max_length=2000),
                    _: dict = Depends(_review_reader)):
    return market_review.realtime_view(after=after)


@router.get('/v1/market/events/index')
def market_event_index(
    run_id: str | None = Query(default=None, min_length=1, max_length=200),
    after: str | None = Query(default=None, max_length=2000),
    limit: int = Query(50, ge=1, le=200),
    scope: str = Query('run', pattern='^(run|today|history)$'),
    date_from: str | None = Query(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$'),
    date_to: str | None = Query(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$'),
    symbol: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None, max_length=100),
    _: dict = Depends(_review_reader),
):
    # `run_id` keeps the existing observation desk contract. Split pages use
    # trading-day scopes and therefore intentionally do not need /latest.
    if scope == 'run' and not run_id:
        raise HTTPException(422, 'run_id is required when scope=run')
    return market_review.event_index(run_id=run_id, after=after, limit=limit,
                                     scope=scope, date_from=date_from, date_to=date_to,
                                     symbol=symbol, status=status)


@router.get('/v1/market/events/{event_id}/detail')
def market_event_detail(event_id: str,
                        _: dict = Depends(_review_reader)):
    return market_review.event_detail(event_id)


@router.get('/v1/market/series')
def market_series(symbol: str = Query(min_length=1,max_length=100), start: str | None = None,
                  end: str | None = None, bucket_ms: int = 1000, after: str | None = None,
                  run_id: str | None = Query(default=None, max_length=200),
                  _: dict = Depends(_review_reader)):
    return market_review.series(symbol,start,end,bucket_ms,after,run_id)


@router.get('/v1/market/events')
def market_events(run_id: str = Query(min_length=1,max_length=200),
                  after_sequence: int = Query(0,ge=0), limit: int = Query(500,ge=1,le=2000),
                  _: dict = Depends(_review_reader)):
    return market_review.events_page(run_id,after_sequence,limit)


@router.get('/v1/market/alerts')
def market_alerts(run_id: str = Query(min_length=1,max_length=200),
                  after_sequence: int = Query(0,ge=0), limit: int = Query(200,ge=1,le=500),
                  user: dict = Depends(_review_reader)):
    return market_review.alert_state(run_id, int(user['id']), after_sequence, limit)


@router.post('/v1/market/alerts/{event_id}/read')
def market_alert_read(event_id: str, run_id: str = Query(min_length=1,max_length=200),
                      user: dict = Depends(_review_reader)):
    return market_review.mark_alert_read(run_id, event_id, int(user['id']))


@router.get('/v1/market/positions/{position_id}')
def market_position(position_id: str, run_id: str = Query(min_length=1,max_length=200),
                    _: dict = Depends(_review_reader)):
    return market_review.position_view(position_id,run_id)


@router.get('/v1/market/comparison')
def market_comparison(symbol: str = Query(min_length=1,max_length=100), start: str | None = None,
                      end: str | None = None, run_id: str | None = Query(default=None, max_length=200),
                      _: dict = Depends(_review_reader)):
    return market_review.comparison(symbol,start,end,run_id)


@router.get('/v1/market/annotations')
def market_annotations(position_id: str, run_id: str, after_id: int = Query(0,ge=0),
                       _: dict = Depends(_review_reader)):
    return market_review.annotations(position_id,run_id,after_id)


@router.post('/v1/market/annotations')
def market_annotate(body: dict, user: dict = Depends(_review_reader)):
    require_permission('market.annotate',user)
    return market_review.annotate(body,user)
