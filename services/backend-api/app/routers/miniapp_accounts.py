"""微信小程序账号申请与 SSO 账号绑定。

小程序只持有微信 OpenID；用户、角色、密码仍由现有 SSO/RBAC 体系管理。
云函数以独立服务令牌调用公开给小程序的接口，管理员接口继续使用本站管理员会话。
"""

from __future__ import annotations

import hmac
import os
import time
import uuid

from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.core import _audit, _conn, _encode_token, _token_ttl_seconds, _user_roles_permissions, require_admin


router = APIRouter()


def _clean_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _require_miniapp_service(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("MINIAPP_SERVICE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="miniapp service is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid miniapp service token")


class OpenIdRequest(BaseModel):
    openid: str = Field(min_length=1, max_length=128)

    @field_validator("openid")
    @classmethod
    def normalize_openid(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("openid must not be blank")
        return value


class AccountRequestCreate(OpenIdRequest):
    request_type: Literal["bind_existing", "create_new"]
    requested_username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=80)
    department: str | None = Field(default=None, max_length=80)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("requested_username", "display_name")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("department", "reason")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return _clean_text(value)


class AccountRequestReview(BaseModel):
    user_id: int | None = Field(default=None, gt=0)
    review_note: str | None = Field(default=None, max_length=500)

    @field_validator("review_note")
    @classmethod
    def normalize_review_note(cls, value: str | None) -> str | None:
        return _clean_text(value)


def _request_dict(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row[0],
        "request_type": row[1],
        "requested_username": row[2],
        "display_name": row[3],
        "department": row[4],
        "reason": row[5],
        "status": row[6],
        "target_user_id": row[7],
        "review_note": row[8],
        "created_at": row[9],
        "reviewed_at": row[10],
    }


def _account_status(openid: str) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT l.id, l.status, u.id, u.username, u.display_name, u.status, u.is_admin
                FROM miniapp_account_links l
                JOIN users u ON u.id = l.user_id
                WHERE l.openid = %s
                """,
                (openid,),
            )
            link = cur.fetchone()
            cur.execute(
                """
                SELECT id, request_type, requested_username, display_name, department, reason,
                       status, target_user_id, review_note, created_at, reviewed_at
                FROM miniapp_account_requests
                WHERE openid = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (openid,),
            )
            request = _request_dict(cur.fetchone())

    if not link:
        return {"linked": False, "authorized": False, "account": None, "request": request}

    link_active = link[1] == "active" and link[5] == "active"
    roles, permissions = _user_roles_permissions(int(link[2]), bool(link[6]))
    authorized = link_active and ("admin" in roles or "admin.access" in permissions or "formula.read" in permissions)
    return {
        "linked": link_active,
        "authorized": authorized,
        "account": {
            "user_id": link[2],
            "username": link[3],
            "display_name": link[4],
            "status": link[5] if link[1] == "active" else "disabled",
            "roles": roles,
            "permissions": permissions,
        },
        "request": request,
    }


@router.post("/v1/miniapp/account/status", dependencies=[Depends(_require_miniapp_service)])
def miniapp_account_status(body: OpenIdRequest) -> dict[str, Any]:
    return _account_status(body.openid)


@router.post("/v1/miniapp/account/requests", dependencies=[Depends(_require_miniapp_service)])
def miniapp_create_account_request(body: AccountRequestCreate) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM miniapp_account_links WHERE openid = %s AND status = 'active'",
                (body.openid,),
            )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="WeChat identity is already linked")
            cur.execute(
                "SELECT id FROM miniapp_account_requests WHERE openid = %s AND status = 'pending'",
                (body.openid,),
            )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="an account request is already pending")
            cur.execute(
                """
                INSERT INTO miniapp_account_requests(
                    openid, request_type, requested_username, display_name, department, reason
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    body.openid,
                    body.request_type,
                    body.requested_username,
                    body.display_name,
                    body.department,
                    body.reason,
                ),
            )
            request_id = int(cur.fetchone()[0])
        conn.commit()

    _audit("miniapp", "miniapp.account.request.create", "miniapp_account_requests", str(request_id), {"request_type": body.request_type})
    return {"ok": True, "request_id": request_id, "status": "pending"}


@router.post("/v1/miniapp/auth/token", dependencies=[Depends(_require_miniapp_service)])
def miniapp_exchange_token(body: OpenIdRequest) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.username, u.display_name, u.status, u.is_admin, u.token_version
                FROM miniapp_account_links l
                JOIN users u ON u.id = l.user_id
                WHERE l.openid = %s AND l.status = 'active'
                """,
                (body.openid,),
            )
            user = cur.fetchone()
    if not user or user[3] != "active":
        raise HTTPException(status_code=403, detail="account is not linked or active")

    roles, permissions = _user_roles_permissions(int(user[0]), bool(user[4]))
    now_ts = int(time.time())
    payload = {
        "uid": user[0],
        "username": user[1],
        "display_name": user[2],
        "roles": roles,
        "permissions": permissions,
        "tv": int(user[5]),
        "auth_source": "miniapp",
        "jti": uuid.uuid4().hex,
        "iat": now_ts,
        "exp": now_ts + min(_token_ttl_seconds(), 3600),
    }
    return {"token": _encode_token(payload), "expires_in": min(_token_ttl_seconds(), 3600), "permissions": permissions}


@router.get("/v1/admin/miniapp-account-requests")
def admin_miniapp_account_requests(
    status: Literal["pending", "approved", "rejected", "cancelled", "all"] = Query(default="pending"),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    where = "" if status == "all" else "WHERE r.status = %s"
    params: tuple[Any, ...] = () if status == "all" else (status,)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT r.id, r.openid, r.request_type, r.requested_username, r.display_name,
                       r.department, r.reason, r.status, r.target_user_id, r.review_note,
                       r.created_at, r.reviewed_at, u.username, u.display_name
                FROM miniapp_account_requests r
                LEFT JOIN users u ON u.id = r.target_user_id
                {where}
                ORDER BY CASE WHEN r.status = 'pending' THEN 0 ELSE 1 END, r.created_at DESC, r.id DESC
                LIMIT 500
                """,
                params,
            )
            rows = cur.fetchall()
    return {
        "items": [
            {
                "id": row[0], "openid": row[1], "request_type": row[2],
                "requested_username": row[3], "display_name": row[4], "department": row[5],
                "reason": row[6], "status": row[7], "target_user_id": row[8],
                "review_note": row[9], "created_at": row[10], "reviewed_at": row[11],
                "target_username": row[12], "target_display_name": row[13],
            }
            for row in rows
        ]
    }


@router.post("/v1/admin/miniapp-account-requests/{request_id}/approve")
def admin_approve_miniapp_account_request(
    request_id: int,
    body: AccountRequestReview,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if body.user_id is None:
        raise HTTPException(status_code=422, detail="user_id is required; provision the SSO account first")
    actor_id = int(actor.get("uid")) if actor.get("uid") is not None else None
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT openid, status FROM miniapp_account_requests WHERE id = %s FOR UPDATE",
                (request_id,),
            )
            request = cur.fetchone()
            if not request:
                raise HTTPException(status_code=404, detail="account request not found")
            if request[1] != "pending":
                raise HTTPException(status_code=409, detail="account request has already been reviewed")
            cur.execute("SELECT id, status FROM users WHERE id = %s", (body.user_id,))
            user = cur.fetchone()
            if not user or user[1] != "active":
                raise HTTPException(status_code=409, detail="target SSO user does not exist or is inactive")
            cur.execute(
                "SELECT openid FROM miniapp_account_links WHERE user_id = %s AND openid <> %s",
                (body.user_id, request[0]),
            )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="target SSO user is already linked to another WeChat identity")
            cur.execute(
                """
                INSERT INTO miniapp_account_links(openid, user_id, request_id, bound_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (openid) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    request_id = EXCLUDED.request_id,
                    bound_by = EXCLUDED.bound_by,
                    status = 'active',
                    updated_at = NOW()
                """,
                (request[0], body.user_id, request_id, actor_id),
            )
            cur.execute(
                """
                UPDATE miniapp_account_requests
                SET status = 'approved', target_user_id = %s, review_note = %s,
                    reviewed_by = %s, reviewed_at = NOW(), updated_at = NOW()
                WHERE id = %s
                """,
                (body.user_id, body.review_note, actor_id, request_id),
            )
        conn.commit()

    _audit(actor.get("username"), "miniapp.account.request.approve", "miniapp_account_requests", str(request_id), {"user_id": body.user_id})
    return {"ok": True, "request_id": request_id, "status": "approved", "user_id": body.user_id}


@router.post("/v1/admin/miniapp-account-requests/{request_id}/reject")
def admin_reject_miniapp_account_request(
    request_id: int,
    body: AccountRequestReview,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    actor_id = int(actor.get("uid")) if actor.get("uid") is not None else None
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE miniapp_account_requests
                SET status = 'rejected', review_note = %s, reviewed_by = %s,
                    reviewed_at = NOW(), updated_at = NOW()
                WHERE id = %s AND status = 'pending'
                RETURNING id
                """,
                (body.review_note, actor_id, request_id),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=409, detail="account request not found or already reviewed")
        conn.commit()
    _audit(actor.get("username"), "miniapp.account.request.reject", "miniapp_account_requests", str(request_id))
    return {"ok": True, "request_id": request_id, "status": "rejected"}


@router.post("/v1/admin/miniapp-account-links/{user_id}/disable")
def admin_disable_miniapp_account_link(
    user_id: int,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE miniapp_account_links SET status = 'disabled', updated_at = NOW() WHERE user_id = %s AND status = 'active' RETURNING id",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="active miniapp account link not found")
        conn.commit()
    _audit(actor.get("username"), "miniapp.account.link.disable", "users", str(user_id))
    return {"ok": True, "user_id": user_id, "status": "disabled"}
