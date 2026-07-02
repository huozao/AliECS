"""认证与用户管理域：登录注册、功能入口(features)、管理后台的用户/角色/权限/功能/联系人管理与审计日志。"""

from __future__ import annotations

import time
import uuid

from contextlib import closing
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel, Field

from app.core import DEFAULT_FEATURES, _audit, _bootstrap_admin_if_needed, _conn, _encode_token, _token_ttl_seconds, _user_roles_permissions, get_current_user, pwd_ctx, require_admin, require_login


router = APIRouter()

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    note: str | None = None


class CreateUserRequest(BaseModel):
    username: str
    display_name: str
    password: str
    is_admin: bool = False


class PatchUserRequest(BaseModel):
    display_name: str | None = None
    status: str | None = None
    is_admin: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=6)


class CreateRoleRequest(BaseModel):
    code: str
    name: str
    description: str | None = None


class PatchRoleRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class PutRoleIdsRequest(BaseModel):
    role_ids: list[int]


class PutPermissionIdsRequest(BaseModel):
    permission_ids: list[int]


class ManagedContactUpsertRequest(BaseModel):
    channel: str
    peer_id: str
    display_name: str | None = None
    remark: str | None = None
    enabled: bool = True
    project_url: str | None = None
    project_name: str | None = None
    tags: str | None = None
    daily_quota: int | None = None
    notes: str | None = None
    source_sheet: str | None = None


class CreateFeatureRequest(BaseModel):
    code: str
    title: str
    description: str | None = None
    url: str | None = None
    category: str | None = None
    required_permission: str | None = None
    status: str = "active"
    sort_order: int = 100


class PatchFeatureRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    url: str | None = None
    category: str | None = None
    required_permission: str | None = None
    status: str | None = None
    sort_order: int | None = None


@router.post("/v1/auth/login")
def auth_login(body: LoginRequest) -> dict[str, Any]:
    _bootstrap_admin_if_needed()

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, display_name, password_hash, status, is_admin, token_version
                FROM users
                WHERE username = %s
                """,
                (body.username,),
            )
            row = cur.fetchone()

            try:
                verified = bool(row) and row[4] == "active" and pwd_ctx.verify(body.password, row[3])
            except Exception:
                verified = False
            if not verified:
                raise HTTPException(status_code=401, detail="invalid credentials")

            user_id = row[0]
            roles, permissions = _user_roles_permissions(user_id, bool(row[5]))

            now = int(time.time())
            payload = {
                "sub": row[1],
                "uid": user_id,
                "display_name": row[2],
                "roles": roles,
                "permissions": permissions,
                "tv": int(row[6]),
                "jti": uuid.uuid4().hex,
                "iat": now,
                "exp": now + _token_ttl_seconds(),
            }

            cur.execute(
                "UPDATE users SET last_login_at = NOW(), updated_at = NOW() WHERE id = %s",
                (user_id,),
            )

        conn.commit()

    _audit(row[1], "auth.login")
    return {
        "token": _encode_token(payload),
        "expires_in": _token_ttl_seconds(),
        "user": {
            "username": row[1],
            "display_name": row[2],
            "roles": roles,
            "permissions": permissions,
        },
    }


@router.post("/v1/auth/register")
def auth_register(body: RegisterRequest) -> dict[str, Any]:
    _audit(body.username, "auth.register.request", detail={"note": body.note})
    return {
        "status": "pending_review",
        "message": "注册请求已提交，请联系管理员在后台分配账号权限。",
        "requested_user": body.username,
    }


@router.post("/v1/auth/logout")
def auth_logout(_: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    return {"status": "ok"}


@router.get("/v1/auth/me")
def auth_me(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    return user


@router.get("/v1/features")
def features(
    authorization: str | None = Header(default=None),
    include_all: bool = Query(default=False),
) -> dict[str, Any]:
    guest = True
    user: dict[str, Any] = {"sub": "guest", "roles": ["guest"], "permissions": []}

    if authorization:
        try:
            user = get_current_user(authorization)
            guest = False
        except HTTPException:
            guest = True

    is_admin = "admin" in user.get("roles", []) or "admin.access" in user.get("permissions", [])

    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, code, title, description, url, category, required_permission, status, sort_order
                    FROM features
                    WHERE status != 'disabled'
                    ORDER BY sort_order ASC, id ASC
                    """
                )
                rows = cur.fetchall()
    except Exception:
        # 兼容迁移尚未完成场景（features 表缺失）：
        # 首页仍返回 200 与用户态，并回退到默认功能列表，避免首页空白。
        rows = [
            (
                feature["id"],
                feature["code"],
                feature["title"],
                feature["description"],
                feature["url"],
                feature["category"],
                feature["required_permission"],
                feature["status"],
                feature["sort_order"],
            )
            for feature in DEFAULT_FEATURES
        ]

    items = []
    for row in rows:
        required_permission = row[6]
        allowed = False

        if is_admin:
            allowed = True
        elif required_permission is None:
            allowed = True
        elif not guest and required_permission in user.get("permissions", []):
            allowed = True

        if include_all or allowed:
            items.append(
                {
                    "id": row[0],
                    "code": row[1],
                    "title": row[2],
                    "description": row[3],
                    "url": row[4],
                    "category": row[5],
                    "required_permission": required_permission,
                    "status": row[7],
                    "sort_order": row[8],
                    "allowed": allowed,
                }
            )

    return {
        "user": {
            "username": user.get("sub", "guest"),
            "roles": user.get("roles", []),
            "permissions": user.get("permissions", []),
        },
        "features": items,
    }


@router.get("/v1/admin/rbac-overview")
def admin_rbac_overview(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, code, name, description FROM roles ORDER BY id")
            roles = cur.fetchall()
            cur.execute(
                """
                SELECT r.id, p.code
                FROM roles r
                LEFT JOIN role_permissions rp ON rp.role_id = r.id
                LEFT JOIN permissions p ON p.id = rp.permission_id
                ORDER BY r.id, p.code
                """
            )
            rows = cur.fetchall()

    mapping: dict[int, list[str]] = {}
    for role_id, perm_code in rows:
        mapping.setdefault(role_id, [])
        if perm_code:
            mapping[role_id].append(perm_code)

    return {
        "roles": [
            {
                "id": r[0],
                "code": r[1],
                "name": r[2],
                "description": r[3],
                "permissions": mapping.get(r[0], []),
            }
            for r in roles
        ]
    }


@router.get("/v1/admin/users")
def admin_users(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, display_name, status, is_admin, created_at, last_login_at
                FROM users
                ORDER BY id
                """
            )
            users = cur.fetchall()

            cur.execute(
                """
                SELECT ur.user_id, r.id, r.code, r.name
                FROM user_roles ur
                JOIN roles r ON r.id = ur.role_id
                ORDER BY r.id
                """
            )
            role_rows = cur.fetchall()

    roles_by_user: dict[int, list[dict[str, Any]]] = {}
    for user_id, role_id, code, name in role_rows:
        roles_by_user.setdefault(user_id, []).append({"id": role_id, "code": code, "name": name})

    return {
        "items": [
            {
                "id": row[0],
                "username": row[1],
                "display_name": row[2],
                "status": row[3],
                "is_admin": row[4],
                "created_at": str(row[5]),
                "last_login_at": str(row[6]) if row[6] else None,
                "roles": roles_by_user.get(row[0], []),
            }
            for row in users
        ]
    }


@router.get("/v1/admin/contacts")
def admin_contacts(channel: str | None = Query(default=None), _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    where = ""
    params: tuple[Any, ...] = ()
    if channel:
        where = "WHERE channel = %s"
        params = (channel,)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, channel, peer_id, display_name, remark, enabled, project_url,
                       project_name, tags, daily_quota, notes, source_sheet, updated_at
                FROM managed_contacts
                {where}
                ORDER BY channel, peer_id
                """,
                params,
            )
            rows = cur.fetchall()
    return {
        "items": [
            {
                "id": row[0],
                "channel": row[1],
                "peer_id": row[2],
                "display_name": row[3],
                "remark": row[4],
                "enabled": row[5],
                "project_url": row[6],
                "project_name": row[7],
                "tags": row[8],
                "daily_quota": row[9],
                "notes": row[10],
                "source_sheet": row[11],
                "updated_at": str(row[12]),
            }
            for row in rows
        ]
    }


@router.post("/v1/admin/contacts")
def admin_upsert_contact(body: ManagedContactUpsertRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    if body.channel not in {"wechat", "feishu"}:
        raise HTTPException(status_code=400, detail="invalid channel")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO managed_contacts(
                    channel, peer_id, display_name, remark, enabled, project_url,
                    project_name, tags, daily_quota, notes, source_sheet, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(channel, peer_id)
                DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    remark = EXCLUDED.remark,
                    enabled = EXCLUDED.enabled,
                    project_url = EXCLUDED.project_url,
                    project_name = EXCLUDED.project_name,
                    tags = EXCLUDED.tags,
                    daily_quota = EXCLUDED.daily_quota,
                    notes = EXCLUDED.notes,
                    source_sheet = EXCLUDED.source_sheet,
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    body.channel,
                    body.peer_id,
                    body.display_name,
                    body.remark,
                    body.enabled,
                    body.project_url,
                    body.project_name,
                    body.tags,
                    body.daily_quota,
                    body.notes,
                    body.source_sheet,
                ),
            )
            contact_id = cur.fetchone()[0]
        conn.commit()
    _audit(actor.get("sub"), "admin.contacts.upsert", "managed_contacts", str(contact_id), body.model_dump())
    return {"id": contact_id}


@router.post("/v1/admin/users")
def admin_create_user(body: CreateUserRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin, status)
                VALUES (%s, %s, %s, %s, 'active')
                RETURNING id
                """,
                (body.username, body.display_name, pwd_ctx.hash(body.password), body.is_admin),
            )
            user_id = cur.fetchone()[0]
        conn.commit()

    _audit(actor.get("sub"), "admin.users.create", "users", str(user_id))
    return {"id": user_id}


@router.patch("/v1/admin/users/{user_id}")
def admin_patch_user(
    user_id: int,
    body: PatchUserRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    fields = body.model_dump(exclude_none=True)
    if not fields:
        return {"status": "ok"}

    allowed = {"display_name", "status", "is_admin"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    sets = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values()) + [user_id]

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE users SET {sets}, updated_at = NOW() WHERE id = %s", values)
        conn.commit()

    _audit(actor.get("sub"), "admin.users.patch", "users", str(user_id), fields)
    return {"status": "ok"}


@router.post("/v1/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    body: ResetPasswordRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s",
                (pwd_ctx.hash(body.new_password), user_id),
            )
        conn.commit()

    _audit(actor.get("sub"), "admin.users.reset_password", "users", str(user_id))
    return {"status": "ok"}


@router.post("/v1/admin/users/{user_id}/revoke-sessions")
def admin_revoke_user_sessions(user_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET token_version = token_version + 1, updated_at = NOW() WHERE id = %s RETURNING token_version",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="user not found")
        conn.commit()

    _audit(user.get("sub"), "admin.users.revoke_sessions", "users", str(user_id))
    return {"user_id": user_id, "token_version": int(row[0])}


@router.post("/v1/admin/users/{user_id}/disable")
def admin_disable_user(user_id: int, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET status = 'disabled', updated_at = NOW() WHERE id = %s", (user_id,))
        conn.commit()

    _audit(actor.get("sub"), "admin.users.disable", "users", str(user_id))
    return {"status": "ok"}


@router.post("/v1/admin/users/{user_id}/enable")
def admin_enable_user(user_id: int, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET status = 'active', updated_at = NOW() WHERE id = %s", (user_id,))
        conn.commit()

    _audit(actor.get("sub"), "admin.users.enable", "users", str(user_id))
    return {"status": "ok"}


@router.get("/v1/admin/roles")
def admin_roles(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, code, name, description FROM roles ORDER BY id")
            rows = cur.fetchall()

    return {
        "items": [
            {"id": row[0], "code": row[1], "name": row[2], "description": row[3]}
            for row in rows
        ]
    }


@router.post("/v1/admin/roles")
def admin_create_role(body: CreateRoleRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO roles(code, name, description) VALUES (%s, %s, %s) RETURNING id",
                (body.code, body.name, body.description),
            )
            role_id = cur.fetchone()[0]
        conn.commit()

    _audit(actor.get("sub"), "admin.roles.create", "roles", str(role_id))
    return {"id": role_id}


@router.patch("/v1/admin/roles/{role_id}")
def admin_patch_role(
    role_id: int,
    body: PatchRoleRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    fields = body.model_dump(exclude_none=True)
    if not fields:
        return {"status": "ok"}

    sets = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values()) + [role_id]

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE roles SET {sets} WHERE id = %s", values)
        conn.commit()

    _audit(actor.get("sub"), "admin.roles.patch", "roles", str(role_id), fields)
    return {"status": "ok"}


@router.get("/v1/admin/permissions")
def admin_permissions(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, code, name, description FROM permissions ORDER BY id")
            rows = cur.fetchall()

    return {
        "items": [
            {"id": row[0], "code": row[1], "name": row[2], "description": row[3]}
            for row in rows
        ]
    }


@router.put("/v1/admin/users/{user_id}/roles")
def admin_set_user_roles(
    user_id: int,
    body: PutRoleIdsRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
            for role_id in body.role_ids:
                cur.execute(
                    """
                    INSERT INTO user_roles(user_id, role_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (user_id, role_id),
                )
        conn.commit()

    _audit(actor.get("sub"), "admin.users.set_roles", "users", str(user_id), {"role_ids": body.role_ids})
    return {"status": "ok"}


@router.put("/v1/admin/roles/{role_id}/permissions")
def admin_set_role_permissions(
    role_id: int,
    body: PutPermissionIdsRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM role_permissions WHERE role_id = %s", (role_id,))
            for permission_id in body.permission_ids:
                cur.execute(
                    """
                    INSERT INTO role_permissions(role_id, permission_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (role_id, permission_id),
                )
        conn.commit()

    _audit(
        actor.get("sub"),
        "admin.roles.set_permissions",
        "roles",
        str(role_id),
        {"permission_ids": body.permission_ids},
    )
    return {"status": "ok"}


@router.get("/v1/admin/features")
def admin_features(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, code, title, description, url, category, required_permission, status, sort_order
                FROM features
                ORDER BY sort_order, id
                """
            )
            rows = cur.fetchall()

    return {
        "items": [
            {
                "id": row[0],
                "code": row[1],
                "title": row[2],
                "description": row[3],
                "url": row[4],
                "category": row[5],
                "required_permission": row[6],
                "status": row[7],
                "sort_order": row[8],
            }
            for row in rows
        ]
    }


@router.post("/v1/admin/features")
def admin_create_feature(
    body: CreateFeatureRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO features(
                    code, title, description, url, category, required_permission, status, sort_order, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                RETURNING id
                """,
                (
                    body.code,
                    body.title,
                    body.description,
                    body.url,
                    body.category,
                    body.required_permission,
                    body.status,
                    body.sort_order,
                ),
            )
            feature_id = cur.fetchone()[0]
        conn.commit()

    _audit(actor.get("sub"), "admin.features.create", "features", str(feature_id))
    return {"id": feature_id}


@router.patch("/v1/admin/features/{feature_id}")
def admin_patch_feature(
    feature_id: int,
    body: PatchFeatureRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    fields = body.model_dump(exclude_none=True)
    if not fields:
        return {"status": "ok"}

    allowed = {"title", "description", "url", "category", "required_permission", "status", "sort_order"}
    fields = {k: v for k, v in fields.items() if k in allowed}

    sets = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values()) + [feature_id]

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE features SET {sets}, updated_at = NOW() WHERE id = %s", values)
        conn.commit()

    _audit(actor.get("sub"), "admin.features.patch", "features", str(feature_id), fields)
    return {"status": "ok"}


@router.get("/v1/admin/audit-logs")
def admin_audit_logs(
    page: int = Query(default=1),
    page_size: int = Query(default=50),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    offset = (page - 1) * page_size
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_logs")
            total = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT id, actor_username, action, target_type, target_id, detail, created_at
                FROM audit_logs
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                (page_size, offset),
            )
            rows = cur.fetchall()

    return {
        "items": [
            {
                "id": row[0],
                "actor_username": row[1],
                "action": row[2],
                "target_type": row[3],
                "target_id": row[4],
                "detail": row[5],
                "created_at": str(row[6]),
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
