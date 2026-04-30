from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import closing
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, status
from passlib.context import CryptContext
from pydantic import BaseModel

app = FastAPI(title="backend-api-phase1", version="0.3.0")
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(_database_url(), connect_timeout=3)


def _db_ping() -> tuple[bool, str]:
    if not _database_url():
        return False, "DATABASE_URL is empty"
    try:
        with closing(_conn()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, "db ok"
    except Exception as exc:
        return False, f"db error: {exc}"


def _token_secret() -> str:
    secret = os.getenv("AUTH_TOKEN_SECRET", "change-this-in-production")
    if os.getenv("ENV", "dev") == "prod" and secret == "change-this-in-production":
        raise HTTPException(status_code=500, detail="AUTH_TOKEN_SECRET must be changed in production")
    return secret


def _token_ttl_seconds() -> int:
    return int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "28800"))


def _sign(payload: str) -> str:
    return hmac.new(_token_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def _encode_token(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    b64 = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return f"{b64}.{_sign(b64)}"


def _decode_token(token: str) -> dict[str, Any]:
    try:
        b64, sig = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    if not hmac.compare_digest(sig, _sign(b64)):
        raise HTTPException(status_code=401, detail="invalid token")
    body = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
    payload = json.loads(body)
    if int(payload.get("exp", 0)) <= int(time.time()):
        raise HTTPException(status_code=401, detail="token expired")
    return payload


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing authorization")
    return authorization[7:].strip()


def _audit(actor: str | None, action: str, target_type: str | None = None, target_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
    try:
        with closing(_conn()) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_logs(actor_username,action,target_type,target_id,detail) VALUES(%s,%s,%s,%s,%s)",
                (actor, action, target_type, target_id, json.dumps(detail or {})),
            )
            conn.commit()
    except Exception:
        pass


def _bootstrap_admin_if_needed() -> None:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        if cur.fetchone()[0] > 0:
            return
        username = os.getenv("ADMIN_BOOTSTRAP_USERNAME", "admin")
        password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "admin123")
        display_name = os.getenv("ADMIN_BOOTSTRAP_DISPLAY_NAME", "系统管理员")
        pw_hash = pwd_ctx.hash(password)
        cur.execute(
            "INSERT INTO users(username,display_name,password_hash,is_admin,status) VALUES(%s,%s,%s,true,'active') RETURNING id",
            (username, display_name, pw_hash),
        )
        user_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM roles WHERE code='admin'")
        role = cur.fetchone()
        if role:
            cur.execute("INSERT INTO user_roles(user_id,role_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (user_id, role[0]))
        conn.commit()


def _user_roles_permissions(user_id: int) -> tuple[list[str], list[str]]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT r.code FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=%s", (user_id,))
        roles = [r[0] for r in cur.fetchall()]
        cur.execute(
            "SELECT DISTINCT p.code FROM permissions p JOIN role_permissions rp ON rp.permission_id=p.id JOIN user_roles ur ON ur.role_id=rp.role_id WHERE ur.user_id=%s",
            (user_id,),
        )
        perms = [p[0] for p in cur.fetchall()]
        return roles, perms


def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _extract_bearer(authorization)
    payload = _decode_token(token)
    return payload


def require_login(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return user


def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if "admin.access" in user.get("permissions", []) or "admin" in user.get("roles", []):
        return user
    raise HTTPException(status_code=403, detail="permission denied")


def require_permission(code: str):
    def _checker(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if code in user.get("permissions", []) or "admin" in user.get("roles", []):
            return user
        raise HTTPException(status_code=403, detail="permission denied")

    return _checker


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    note: str | None = None


@app.get("/healthz")
def healthz() -> dict[str, object]:
    db_ok, db_message = _db_ping()
    return {"status": "ok" if db_ok else "degraded", "service": "backend-api", "database": {"ok": db_ok, "message": db_message}}


@app.get("/readyz")
def readyz() -> dict[str, object]:
    db_ok, db_message = _db_ping()
    return {"status": "ready"} if db_ok else {"status": "not-ready", "reason": db_message}


@app.get("/v1/ping")
def ping() -> dict[str, str]:
    return {"message": "pong"}


@app.post("/v1/auth/login")
def auth_login(body: LoginRequest) -> dict[str, Any]:
    _bootstrap_admin_if_needed()
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,username,display_name,password_hash,status,is_admin FROM users WHERE username=%s", (body.username,))
        row = cur.fetchone()
        if not row or row[4] != "active" or not pwd_ctx.verify(body.password, row[3]):
            raise HTTPException(status_code=401, detail="invalid credentials")
        user_id = row[0]
        roles, perms = _user_roles_permissions(user_id)
        now = int(time.time())
        payload = {"sub": row[1], "uid": user_id, "display_name": row[2], "roles": roles, "permissions": perms, "iat": now, "exp": now + _token_ttl_seconds()}
        cur.execute("UPDATE users SET last_login_at=NOW(),updated_at=NOW() WHERE id=%s", (user_id,))
        conn.commit()
    _audit(row[1], "auth.login")
    return {"token": _encode_token(payload), "expires_in": _token_ttl_seconds(), "user": {"username": row[1], "display_name": row[2], "roles": roles, "permissions": perms}}


@app.post("/v1/auth/register")
def auth_register(body: RegisterRequest) -> dict[str, Any]:
    _audit(body.username, "auth.register.request", detail={"note": body.note})
    return {"status": "pending_review", "message": "注册请求已提交，请联系管理员在后台分配账号权限。"}


@app.post("/v1/auth/logout")
def auth_logout(_: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/auth/register")
def auth_register(body: RegisterRequest) -> dict[str, Any]:
    users = _users()
    if body.username in users:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username already exists")

    # 当前阶段不直接写入数据库。注册请求返回待审批，后续由后台分配权限。
    return {
        "status": "pending_review",
        "message": "注册请求已提交，请联系管理员在后台分配账号权限。",
        "requested_user": body.username,
    }


@app.get("/v1/auth/me")
def auth_me(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    return user


@app.get("/v1/features")
def features(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    guest = True
    user = {"sub": "guest", "roles": ["guest"], "permissions": []}
    if authorization:
        try:
            user = get_current_user(authorization)
            guest = False
        except HTTPException:
            guest = True
    is_admin = "admin" in user.get("roles", []) or "admin.access" in user.get("permissions", [])
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,code,title,description,url,category,required_permission,status,sort_order FROM features ORDER BY sort_order ASC,id ASC")
        rows = cur.fetchall()
    items = []
    for r in rows:
        req = r[6]
        allowed = (not req and not guest) or (req in user.get("permissions", [])) or is_admin
        if guest:
            allowed = req is None
        if allowed:
            items.append({"id": r[0], "code": r[1], "title": r[2], "description": r[3], "url": r[4], "category": r[5], "required_permission": req, "status": r[7], "sort_order": r[8]})
    return {"user": {"username": user.get("sub"), "roles": user.get("roles", []), "permissions": user.get("permissions", [])}, "features": items}


@app.get("/v1/admin/users")
def admin_users(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,username,display_name,status,is_admin,created_at,last_login_at FROM users ORDER BY id")
        users = cur.fetchall()
    return {"items": [{"id": u[0], "username": u[1], "display_name": u[2], "status": u[3], "is_admin": u[4], "created_at": str(u[5]), "last_login_at": str(u[6]) if u[6] else None} for u in users]}


@app.post("/v1/admin/users")
def admin_create_user(body: CreateUserRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO users(username,display_name,password_hash,is_admin,status) VALUES(%s,%s,%s,%s,'active') RETURNING id", (body.username, body.display_name, pwd_ctx.hash(body.password), body.is_admin))
        uid = cur.fetchone()[0]
        conn.commit()
    _audit(actor.get("sub"), "admin.users.create", "users", str(uid))
    return {"id": uid}


@app.patch("/v1/admin/users/{user_id}")
def admin_patch_user(user_id: int, body: PatchUserRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        if body.display_name is not None:
            cur.execute("UPDATE users SET display_name=%s,updated_at=NOW() WHERE id=%s", (body.display_name, user_id))
        conn.commit()
    _audit(actor.get("sub"), "admin.users.patch", "users", str(user_id), body.model_dump(exclude_none=True))
    return {"status": "ok"}


@app.post("/v1/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, body: ResetPasswordRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET password_hash=%s,updated_at=NOW() WHERE id=%s", (pwd_ctx.hash(body.new_password), user_id))
        conn.commit()
    _audit(actor.get("sub"), "admin.users.reset_password", "users", str(user_id))
    return {"status": "ok"}


@app.post("/v1/admin/users/{user_id}/disable")
def admin_disable_user(user_id: int, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET status='disabled',updated_at=NOW() WHERE id=%s", (user_id,))
        conn.commit()
    _audit(actor.get("sub"), "admin.users.disable", "users", str(user_id))
    return {"status": "ok"}


@app.post("/v1/admin/users/{user_id}/enable")
def admin_enable_user(user_id: int, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET status='active',updated_at=NOW() WHERE id=%s", (user_id,))
        conn.commit()
    _audit(actor.get("sub"), "admin.users.enable", "users", str(user_id))
    return {"status": "ok"}


@app.get("/v1/admin/roles")
def admin_roles(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,code,name,description FROM roles ORDER BY id")
        rows = cur.fetchall()
    return {"items": [{"id": r[0], "code": r[1], "name": r[2], "description": r[3]} for r in rows]}


@app.post("/v1/admin/roles")
def admin_create_role(body: CreateRoleRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO roles(code,name,description) VALUES(%s,%s,%s) RETURNING id", (body.code, body.name, body.description))
        rid = cur.fetchone()[0]
        conn.commit()
    _audit(actor.get("sub"), "admin.roles.create", "roles", str(rid))
    return {"id": rid}


@app.patch("/v1/admin/roles/{role_id}")
def admin_patch_role(role_id: int, body: PatchRoleRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        if body.name is not None:
            cur.execute("UPDATE roles SET name=%s WHERE id=%s", (body.name, role_id))
        if body.description is not None:
            cur.execute("UPDATE roles SET description=%s WHERE id=%s", (body.description, role_id))
        conn.commit()
    _audit(actor.get("sub"), "admin.roles.patch", "roles", str(role_id), body.model_dump(exclude_none=True))
    return {"status": "ok"}


@app.get("/v1/admin/permissions")
def admin_permissions(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,code,name,description FROM permissions ORDER BY id")
        rows = cur.fetchall()
    return {"items": [{"id": r[0], "code": r[1], "name": r[2], "description": r[3]} for r in rows]}


@app.put("/v1/admin/users/{user_id}/roles")
def admin_set_user_roles(user_id: int, body: PutRoleIdsRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM user_roles WHERE user_id=%s", (user_id,))
        for rid in body.role_ids:
            cur.execute("INSERT INTO user_roles(user_id,role_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (user_id, rid))
        conn.commit()
    _audit(actor.get("sub"), "admin.users.set_roles", "users", str(user_id), {"role_ids": body.role_ids})
    return {"status": "ok"}


@app.put("/v1/admin/roles/{role_id}/permissions")
def admin_set_role_permissions(role_id: int, body: PutPermissionIdsRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM role_permissions WHERE role_id=%s", (role_id,))
        for pid in body.permission_ids:
            cur.execute("INSERT INTO role_permissions(role_id,permission_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (role_id, pid))
        conn.commit()
    _audit(actor.get("sub"), "admin.roles.set_permissions", "roles", str(role_id), {"permission_ids": body.permission_ids})
    return {"status": "ok"}


@app.get("/v1/admin/features")
def admin_features(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,code,title,description,url,category,required_permission,status,sort_order FROM features ORDER BY sort_order,id")
        rows = cur.fetchall()
    return {"items": [{"id": r[0], "code": r[1], "title": r[2], "description": r[3], "url": r[4], "category": r[5], "required_permission": r[6], "status": r[7], "sort_order": r[8]} for r in rows]}


@app.post("/v1/admin/features")
def admin_create_feature(body: CreateFeatureRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO features(code,title,description,url,category,required_permission,status,sort_order,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW()) RETURNING id",
            (body.code, body.title, body.description, body.url, body.category, body.required_permission, body.status, body.sort_order),
        )
        fid = cur.fetchone()[0]
        conn.commit()
    _audit(actor.get("sub"), "admin.features.create", "features", str(fid))
    return {"id": fid}


@app.patch("/v1/admin/features/{feature_id}")
def admin_patch_feature(feature_id: int, body: PatchFeatureRequest, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    fields = body.model_dump(exclude_none=True)
    if not fields:
        return {"status": "ok"}
    sets = ", ".join(f"{k}=%s" for k in fields.keys()) + ", updated_at=NOW()"
    vals = list(fields.values()) + [feature_id]
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE features SET {sets} WHERE id=%s", vals)
        conn.commit()
    _audit(actor.get("sub"), "admin.features.patch", "features", str(feature_id), fields)
    return {"status": "ok"}


@app.get("/v1/admin/audit-logs")
def admin_audit_logs(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,actor_username,action,target_type,target_id,detail,created_at FROM audit_logs ORDER BY id DESC LIMIT 200")
        rows = cur.fetchall()
    return {"items": [{"id": r[0], "actor_username": r[1], "action": r[2], "target_type": r[3], "target_id": r[4], "detail": r[5], "created_at": str(r[6])} for r in rows]}
