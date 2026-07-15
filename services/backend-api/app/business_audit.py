"""可靠业务审计：普通查询尽力写入，下载/导出等关键操作失败即拒绝。"""

from __future__ import annotations

import hashlib
import os
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from psycopg.types.json import Jsonb

from app.core import _conn, _request_logger


_CHANNELS = {"website", "miniapp", "admin", "machine", "unknown"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _actor_user_id(user: dict[str, Any]) -> int | None:
    value = user.get("uid")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _username(user: dict[str, Any]) -> str:
    return str(user.get("username") or user.get("sub") or "")[:200]


def _channel(user: dict[str, Any], request: Request | None) -> str:
    supplied = str(request.headers.get("X-Client-Channel") or "").strip().lower() if request else ""
    if supplied in _CHANNELS:
        return supplied
    auth_source = str(user.get("auth_source") or "").lower()
    if auth_source == "miniapp":
        return "miniapp"
    if request and request.url.path.startswith("/v1/admin/"):
        return "admin"
    if auth_source in {"service", "machine"}:
        return "machine"
    return "website" if request else "unknown"


def write_business_audit(
    *,
    user: dict[str, Any],
    action: str,
    request: Request | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    resource_revision: str | None = None,
    query: dict[str, Any] | None = None,
    result_count: int | None = None,
    outcome: str = "success",
    error_code: str | None = None,
    file_sha256: str | None = None,
    detail: dict[str, Any] | None = None,
    required: bool = False,
) -> int | None:
    request_id = (
        str(request.headers.get("X-Request-ID") or "").strip()[:160]
        if request else ""
    ) or uuid.uuid4().hex
    client_host = request.client.host if request and request.client else None
    user_agent = str(request.headers.get("User-Agent") or "")[:500] if request else None
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO business_audit_events(
                        actor_user_id, actor_username_snapshot, auth_source, client_channel,
                        action, resource_type, resource_id, resource_revision, query_json,
                        result_count, outcome, error_code, request_id, ip_address, user_agent,
                        file_sha256, detail_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (request_id, action, COALESCE(resource_id, '')) DO UPDATE SET
                        outcome = EXCLUDED.outcome,
                        error_code = EXCLUDED.error_code,
                        result_count = EXCLUDED.result_count,
                        detail_json = EXCLUDED.detail_json
                    RETURNING id
                    """,
                    (
                        _actor_user_id(user), _username(user), str(user.get("auth_source") or "unknown")[:80],
                        _channel(user, request), action[:160], resource_type, resource_id, resource_revision,
                        Jsonb(query or {}), result_count, outcome, error_code, request_id, client_host,
                        user_agent, file_sha256, Jsonb(detail or {}),
                    ),
                )
                audit_id = int(cur.fetchone()[0])
            conn.commit()
        return audit_id
    except Exception as exc:  # noqa: BLE001 - required 决定是否阻断业务
        _request_logger.error(
            "business audit write failed action=%s required=%s error=%s",
            action, required, type(exc).__name__,
        )
        # 单元测试和无数据库的本地开发环境保持可运行；生产永远严格执行 required。
        if required and (os.getenv("ENV", "dev") == "prod" or os.getenv("DATABASE_URL")):
            raise HTTPException(status_code=503, detail="审计记录写入失败，关键操作已拒绝") from exc
        return None
