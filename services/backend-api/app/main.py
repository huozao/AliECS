from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field


app = FastAPI(title="AliECS Backend API", version="0.4.0")

def _cors_origins() -> list[str]:
    defaults = [
        "http://localhost:8080",
        "http://localhost:8081",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8081",
        "https://localhost:8080",
        "https://localhost:8081",
        "https://127.0.0.1:8080",
        "https://127.0.0.1:8081",
    ]
    origins = {x.strip() for x in defaults if x.strip()}

    app_base = os.getenv("APP_BASE_URL", "").strip()
    if app_base:
        origins.add(app_base.rstrip("/"))

    extra = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
    if extra:
        origins.update({x.strip().rstrip("/") for x in extra.split(",") if x.strip()})

    return sorted(origins)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

DEFAULT_FEATURES: list[dict[str, Any]] = [
    {"id": 1, "code": "new_model_form", "title": "新品型号录入表", "description": "新品型号登记", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_4b7094", "category": "业务录入", "required_permission": "production.schedule.write", "status": "active", "sort_order": 10},
    {"id": 2, "code": "schedule_form", "title": "排产登记表", "description": "排产信息登记", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_e3792e", "category": "业务录入", "required_permission": "production.schedule.write", "status": "active", "sort_order": 20},
    {"id": 3, "code": "pending_return_alert", "title": "待处理+退货提醒", "description": "待处理与退货提醒", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_4501d0", "category": "业务录入", "required_permission": "production.schedule.read", "status": "active", "sort_order": 30},
    {"id": 4, "code": "naming_form", "title": "产品命名登记", "description": "产品命名录入", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_a577fc", "category": "业务录入", "required_permission": "formula.write", "status": "active", "sort_order": 40},
    {"id": 5, "code": "qc_form", "title": "检测数据登记表", "description": "检测数据登记", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_b669cf", "category": "质检", "required_permission": "formula.read", "status": "active", "sort_order": 50},
    {"id": 6, "code": "density_calculator", "title": "配方密度计算器", "description": "配方密度工具", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_bac993", "category": "质检", "required_permission": "formula.read", "status": "active", "sort_order": 60},
    {"id": 7, "code": "formula_query", "title": "配方查询", "description": "配方检索入口", "url": None, "category": "业务查询", "required_permission": "formula.read", "status": "reserved", "sort_order": 70},
    {"id": 8, "code": "midea_requirement", "title": "美的需求", "description": "需求查询入口", "url": None, "category": "业务查询", "required_permission": "midea.requirement.read", "status": "reserved", "sort_order": 80},
    {"id": 9, "code": "raw_inventory", "title": "原材料库存", "description": "原材料库存入口", "url": None, "category": "业务查询", "required_permission": "inventory.raw.read", "status": "reserved", "sort_order": 90},
    {"id": 10, "code": "finished_inventory", "title": "成品库存", "description": "成品库存入口", "url": None, "category": "业务查询", "required_permission": "inventory.finished.read", "status": "reserved", "sort_order": 100},
    {"id": 11, "code": "personal_section", "title": "个人板块", "description": "个人工具入口", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_0c521a", "category": "个人", "required_permission": "personal.access", "status": "active", "sort_order": 110},
    {"id": 12, "code": "admin_ui", "title": "Admin UI", "description": "管理后台入口", "url": "/admin/", "category": "系统", "required_permission": "admin.access", "status": "active", "sort_order": 120},
]


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "")


def _conn() -> psycopg.Connection:
    database_url = _database_url()
    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL is empty")
    return psycopg.connect(database_url, connect_timeout=3)


def _db_ping() -> tuple[bool, str]:
    if not _database_url():
        return False, "DATABASE_URL is empty"

    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, "db ok"
    except Exception as exc:
        return False, f"db error: {exc}"


def _token_secret() -> str:
    secret = os.getenv("AUTH_TOKEN_SECRET", "change-this-in-production")
    env_name = os.getenv("ENV", "dev")
    if env_name == "prod" and secret == "change-this-in-production":
        raise HTTPException(status_code=500, detail="AUTH_TOKEN_SECRET must be changed in production")
    return secret


def _token_ttl_seconds() -> int:
    raw = os.getenv("AUTH_TOKEN_TTL_SECONDS", "28800")
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="AUTH_TOKEN_TTL_SECONDS must be integer") from exc


def _sign(payload: str) -> str:
    return hmac.new(_token_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _encode_token(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    b64 = base64.urlsafe_b64encode(body.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"{b64}.{_sign(b64)}"


def _decode_token(token: str) -> dict[str, Any]:
    try:
        b64, sig = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc

    if not hmac.compare_digest(sig, _sign(b64)):
        raise HTTPException(status_code=401, detail="invalid token")

    try:
        body = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
        payload = json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc

    if int(payload.get("exp", 0)) <= int(time.time()):
        raise HTTPException(status_code=401, detail="token expired")

    return payload


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing authorization")
    return authorization[7:].strip()


def _audit(
    actor: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_logs(actor_username, action, target_type, target_id, detail)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (actor, action, target_type, target_id, Jsonb(detail or {})),
                )
            conn.commit()
    except Exception:
        # 审计失败不应阻断主流程。
        pass


def _user_roles_permissions(user_id: int, is_admin: bool = False) -> tuple[list[str], list[str]]:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.code
                    FROM roles r
                    JOIN user_roles ur ON ur.role_id = r.id
                    WHERE ur.user_id = %s
                    ORDER BY r.id
                    """,
                    (user_id,),
                )
                roles = [row[0] for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT DISTINCT p.code
                    FROM permissions p
                    JOIN role_permissions rp ON rp.permission_id = p.id
                    JOIN user_roles ur ON ur.role_id = rp.role_id
                    WHERE ur.user_id = %s
                    ORDER BY p.code
                    """,
                    (user_id,),
                )
                permissions = [row[0] for row in cur.fetchall()]
    except Exception:
        # 兼容旧环境（RBAC 关联表缺失或尚未初始化）：
        # 允许登录继续，按 is_admin 做最小权限回退。
        roles = []
        permissions = []

    if is_admin and "admin" not in roles:
        roles.append("admin")
    if is_admin and "admin.access" not in permissions:
        permissions.append("admin.access")

    return roles, permissions


def _bootstrap_admin_if_needed() -> None:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            user_count = cur.fetchone()[0]
            if user_count > 0:
                return

            username = os.getenv("ADMIN_BOOTSTRAP_USERNAME", "admin")
            password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "admin123")
            display_name = os.getenv("ADMIN_BOOTSTRAP_DISPLAY_NAME", "系统管理员")

            cur.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin, status)
                VALUES (%s, %s, %s, true, 'active')
                RETURNING id
                """,
                (username, display_name, pwd_ctx.hash(password)),
            )
            user_id = cur.fetchone()[0]

            try:
                cur.execute("SELECT id FROM roles WHERE code = 'admin'")
                role_row = cur.fetchone()
                if role_row:
                    cur.execute(
                        """
                        INSERT INTO user_roles(user_id, role_id)
                        VALUES (%s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (user_id, role_row[0]),
                    )
            except Exception:
                # 兼容旧环境（roles / user_roles 尚未迁移完成）：
                # 不阻断管理员账户初始化，登录后由 is_admin 回退授予最小后台权限。
                pass

        conn.commit()

    _audit(username, "auth.bootstrap_admin", "users", str(user_id))


def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _extract_bearer(authorization)
    return _decode_token(token)


def require_login(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return user


def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    roles = user.get("roles", [])
    permissions = user.get("permissions", [])
    if "admin" in roles or "admin.access" in permissions:
        return user
    raise HTTPException(status_code=403, detail="permission denied")


def _couple_feature_enabled() -> bool:
    return os.getenv("COUPLE_FEATURE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def _couple_route() -> str:
    route = os.getenv("COUPLE_ROUTE", "/couple/").strip()
    if not route.startswith("/"):
        return "/couple"
    return route


def _has_couple_access(user: dict[str, Any]) -> bool:
    if not _couple_feature_enabled():
        return False

    permissions = user.get("permissions", [])
    if "couple_memory_access" in permissions:
        return True

    username = str(user.get("username") or user.get("sub") or "").strip().lower()
    email = str(user.get("email") or "").strip().lower()
    roles = [str(r).lower() for r in user.get("roles", [])]
    if "admin" in roles:
        return True

    allowed_users = [i.strip().lower() for i in os.getenv("COUPLE_ALLOWED_USERS", "").split(",") if i.strip()]
    if username and username in allowed_users:
        return True

    allowed_emails = [i.strip().lower() for i in os.getenv("COUPLE_ALLOWED_EMAILS", "").split(",") if i.strip()]
    if email and email in allowed_emails:
        return True

    return False


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


class MemoryUpsertRequest(BaseModel):
    couple_space_id: int | None = None
    title: str
    content: str | None = None
    memory_date: str | None = None
    place_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    cover_photo_url: str | None = None
    visibility: str = "private"
    tags: list[str] = Field(default_factory=list)


class LocalPhotoStorage:
    def __init__(self) -> None:
        self.base_dir = Path(os.getenv("LOCAL_UPLOAD_DIR", "/tmp/aliecs-uploads"))
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def save(self, file: UploadFile) -> dict[str, str]:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise HTTPException(status_code=400, detail="unsupported file type")
        filename = f"{uuid.uuid4().hex}{ext}"
        full_path = self.base_dir / filename
        content = await file.read()
        max_mb = int(os.getenv("PHOTO_MAX_UPLOAD_MB", "10"))
        if len(content) > max_mb * 1024 * 1024:
            raise HTTPException(status_code=400, detail="file too large")
        full_path.write_bytes(content)
        public_base = os.getenv("APP_BASE_URL", "").rstrip("/")
        relative_url = f"/uploads/{filename}"
        return {
            "original_storage_url": str(full_path),
            "display_url": f"{public_base}{relative_url}" if public_base else relative_url,
            "thumbnail_url": f"{public_base}{relative_url}" if public_base else relative_url,
            "storage_driver": "local",
        }


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


class CreateCoupleSpaceRequest(BaseModel):
    name: str
    start_date: str | None = None
    theme: str | None = None


class AddCoupleMemberRequest(BaseModel):
    username: str
    role: str = "member"


@app.get("/healthz")
def healthz() -> dict[str, object]:
    db_ok, db_message = _db_ping()
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "backend-api",
        "database": {"ok": db_ok, "message": db_message},
    }


@app.get("/readyz")
def readyz() -> dict[str, object]:
    db_ok, db_message = _db_ping()
    if db_ok:
        return {"status": "ready"}
    return {"status": "not-ready", "reason": db_message}


@app.get("/v1/ping")
def ping() -> dict[str, str]:
    return {"message": "pong"}


@app.post("/v1/auth/login")
def auth_login(body: LoginRequest) -> dict[str, Any]:
    _bootstrap_admin_if_needed()

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, display_name, password_hash, status, is_admin
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


@app.post("/v1/auth/register")
def auth_register(body: RegisterRequest) -> dict[str, Any]:
    _audit(body.username, "auth.register.request", detail={"note": body.note})
    return {
        "status": "pending_review",
        "message": "注册请求已提交，请联系管理员在后台分配账号权限。",
        "requested_user": body.username,
    }


@app.post("/v1/auth/logout")
def auth_logout(_: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/auth/me")
def auth_me(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    return user


@app.get("/v1/features")
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



@app.get("/v1/admin/rbac-overview")
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


@app.get("/couple/access")
def couple_access(authorization: str | None = Header(default=None)) -> dict[str, Any] | None:
    if not authorization:
        raise HTTPException(status_code=404, detail="not found")

    try:
        user = get_current_user(authorization)
    except HTTPException:
        raise HTTPException(status_code=404, detail="not found")

    if not _has_couple_access(user):
        raise HTTPException(status_code=404, detail="not found")

    return {"allowed": True, "route": _couple_route()}


def _user_id_by_username(username: str) -> int | None:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
    return row[0] if row else None


def _resolve_couple_space_id(user_id: int, requested_space_id: int | None) -> int:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            if requested_space_id:
                cur.execute(
                    """
                    SELECT couple_space_id
                    FROM couple_members
                    WHERE user_id = %s AND couple_space_id = %s
                    """,
                    (user_id, requested_space_id),
                )
                row = cur.fetchone()
                if row:
                    return row[0]
                raise HTTPException(status_code=403, detail="permission denied")

            cur.execute(
                """
                SELECT couple_space_id
                FROM couple_members
                WHERE user_id = %s
                ORDER BY joined_at ASC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if row:
                return row[0]
    raise HTTPException(status_code=404, detail="not found")


@app.get("/v1/memories")
def list_memories(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    offset = (page - 1) * page_size

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM memories WHERE couple_space_id = %s", (space_id,))
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT id, couple_space_id, title, content, memory_date, place_name, latitude, longitude,
                       cover_photo_url, visibility, created_by, created_at, updated_at
                FROM memories
                WHERE couple_space_id = %s
                ORDER BY memory_date DESC NULLS LAST, id DESC
                LIMIT %s OFFSET %s
                """,
                (space_id, page_size, offset),
            )
            rows = cur.fetchall()

            memory_ids = [row[0] for row in rows]
            tags_map: dict[int, list[str]] = {mid: [] for mid in memory_ids}
            if memory_ids:
                cur.execute(
                    "SELECT memory_id, tag FROM memory_tags WHERE memory_id = ANY(%s::bigint[]) ORDER BY id",
                    (memory_ids,),
                )
                for mid, tag in cur.fetchall():
                    tags_map.setdefault(mid, []).append(tag)

    return {
        "items": [
            {
                "id": row[0],
                "couple_space_id": row[1],
                "title": row[2],
                "content": row[3],
                "memory_date": str(row[4]) if row[4] else None,
                "place_name": row[5],
                "latitude": row[6],
                "longitude": row[7],
                "cover_photo_url": row[8],
                "visibility": row[9],
                "created_by": row[10],
                "created_at": str(row[11]),
                "updated_at": str(row[12]),
                "tags": tags_map.get(row[0], []),
            }
            for row in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


@app.post("/v1/memories")
def create_memory(body: MemoryUpsertRequest, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    space_id = _resolve_couple_space_id(user_id, body.couple_space_id)
    if body.visibility not in {"private", "shareable"}:
        raise HTTPException(status_code=400, detail="invalid visibility")

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories(
                    couple_space_id, title, content, memory_date, place_name, latitude, longitude,
                    cover_photo_url, visibility, created_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    space_id,
                    body.title,
                    body.content,
                    body.memory_date,
                    body.place_name,
                    body.latitude,
                    body.longitude,
                    body.cover_photo_url,
                    body.visibility,
                    user_id,
                ),
            )
            memory_id = cur.fetchone()[0]
            for tag in body.tags:
                tag_clean = tag.strip()
                if tag_clean:
                    cur.execute(
                        "INSERT INTO memory_tags(memory_id, tag) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (memory_id, tag_clean),
                    )
        conn.commit()
    return {"id": memory_id}


@app.get("/v1/memories/{memory_id}")
def get_memory(memory_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id, m.couple_space_id, m.title, m.content, m.memory_date, m.place_name, m.latitude, m.longitude,
                       m.cover_photo_url, m.visibility, m.created_by, m.created_at, m.updated_at
                FROM memories m
                JOIN couple_members cm ON cm.couple_space_id = m.couple_space_id
                WHERE m.id = %s AND cm.user_id = %s
                LIMIT 1
                """,
                (memory_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
            cur.execute("SELECT tag FROM memory_tags WHERE memory_id = %s ORDER BY id", (memory_id,))
            tags = [r[0] for r in cur.fetchall()]
    return {
        "id": row[0],
        "couple_space_id": row[1],
        "title": row[2],
        "content": row[3],
        "memory_date": str(row[4]) if row[4] else None,
        "place_name": row[5],
        "latitude": row[6],
        "longitude": row[7],
        "cover_photo_url": row[8],
        "visibility": row[9],
        "created_by": row[10],
        "created_at": str(row[11]),
        "updated_at": str(row[12]),
        "tags": tags,
    }


@app.put("/v1/memories/{memory_id}")
def update_memory(memory_id: int, body: MemoryUpsertRequest, user: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    if body.visibility not in {"private", "shareable"}:
        raise HTTPException(status_code=400, detail="invalid visibility")

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.couple_space_id
                FROM memories m
                JOIN couple_members cm ON cm.couple_space_id = m.couple_space_id
                WHERE m.id = %s AND cm.user_id = %s
                LIMIT 1
                """,
                (memory_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
            cur.execute(
                """
                UPDATE memories
                SET title=%s, content=%s, memory_date=%s, place_name=%s, latitude=%s, longitude=%s,
                    cover_photo_url=%s, visibility=%s, updated_at=NOW()
                WHERE id=%s
                """,
                (
                    body.title,
                    body.content,
                    body.memory_date,
                    body.place_name,
                    body.latitude,
                    body.longitude,
                    body.cover_photo_url,
                    body.visibility,
                    memory_id,
                ),
            )
            cur.execute("DELETE FROM memory_tags WHERE memory_id = %s", (memory_id,))
            for tag in body.tags:
                tag_clean = tag.strip()
                if tag_clean:
                    cur.execute("INSERT INTO memory_tags(memory_id, tag) VALUES (%s, %s)", (memory_id, tag_clean))
        conn.commit()
    return {"status": "ok"}


@app.delete("/v1/memories/{memory_id}")
def delete_memory(memory_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM memories m
                USING couple_members cm
                WHERE m.id = %s AND cm.user_id = %s AND cm.couple_space_id = m.couple_space_id
                """,
                (memory_id, user_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    return {"status": "ok"}


@app.get("/v1/map/memories")
def map_memories(
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, memory_date, place_name, latitude, longitude, cover_photo_url
                FROM memories
                WHERE couple_space_id = %s AND latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY memory_date DESC NULLS LAST, id DESC
                """,
                (space_id,),
            )
            rows = cur.fetchall()
    return {
        "items": [
            {
                "id": row[0],
                "title": row[1],
                "memory_date": str(row[2]) if row[2] else None,
                "place_name": row[3],
                "latitude": row[4],
                "longitude": row[5],
                "cover_photo_url": row[6],
            }
            for row in rows
        ]
    }


@app.post("/v1/photos/upload")
async def upload_photo(
    file: UploadFile = File(...),
    memory_id: int | None = Query(default=None),
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    if memory_id is not None:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM memories
                    WHERE id = %s AND couple_space_id = %s
                    LIMIT 1
                    """,
                    (memory_id, space_id),
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=400, detail="memory_id not in current couple space")
    saved = await LocalPhotoStorage().save(file)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO photos(
                    couple_space_id, memory_id, original_filename, original_storage_url,
                    thumbnail_url, display_url, storage_driver
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (
                    space_id,
                    memory_id,
                    file.filename,
                    saved["original_storage_url"],
                    saved["thumbnail_url"],
                    saved["display_url"],
                    saved["storage_driver"],
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return {
        "id": row[0],
        "created_at": str(row[1]),
        "memory_id": memory_id,
        "display_url": saved["display_url"],
        "thumbnail_url": saved["thumbnail_url"],
        "storage_driver": saved["storage_driver"],
    }


@app.get("/v1/photos")
def list_photos(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    offset = (page - 1) * page_size
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM photos WHERE couple_space_id = %s", (space_id,))
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT id, memory_id, original_filename, display_url, thumbnail_url, taken_at, created_at
                FROM photos
                WHERE couple_space_id = %s
                ORDER BY COALESCE(taken_at, created_at) DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (space_id, page_size, offset),
            )
            rows = cur.fetchall()
    return {
        "items": [
            {
                "id": row[0],
                "memory_id": row[1],
                "original_filename": row[2],
                "display_url": row[3],
                "thumbnail_url": row[4],
                "taken_at": str(row[5]) if row[5] else None,
                "created_at": str(row[6]),
            }
            for row in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


@app.get("/v1/photos/{photo_id}")
def get_photo(photo_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.memory_id, p.original_filename, p.original_storage_url, p.display_url, p.thumbnail_url,
                       p.storage_driver, p.created_at
                FROM photos p
                JOIN couple_members cm ON cm.couple_space_id = p.couple_space_id
                WHERE p.id = %s AND cm.user_id = %s
                LIMIT 1
                """,
                (photo_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
    return {
        "id": row[0],
        "memory_id": row[1],
        "original_filename": row[2],
        "original_storage_url": row[3],
        "display_url": row[4],
        "thumbnail_url": row[5],
        "storage_driver": row[6],
        "created_at": str(row[7]),
    }


@app.delete("/v1/photos/{photo_id}")
def delete_photo(photo_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM photos p
                USING couple_members cm
                WHERE p.id = %s AND cm.user_id = %s AND cm.couple_space_id = p.couple_space_id
                RETURNING p.original_storage_url
                """,
                (photo_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    try:
        if row[0]:
            Path(row[0]).unlink(missing_ok=True)
    except Exception:
        pass
    return {"status": "ok"}

@app.get("/v1/admin/users")
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


@app.post("/v1/admin/users")
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


@app.patch("/v1/admin/users/{user_id}")
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


@app.post("/v1/admin/users/{user_id}/reset-password")
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


@app.post("/v1/admin/users/{user_id}/disable")
def admin_disable_user(user_id: int, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET status = 'disabled', updated_at = NOW() WHERE id = %s", (user_id,))
        conn.commit()

    _audit(actor.get("sub"), "admin.users.disable", "users", str(user_id))
    return {"status": "ok"}


@app.post("/v1/admin/users/{user_id}/enable")
def admin_enable_user(user_id: int, actor: dict[str, Any] = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET status = 'active', updated_at = NOW() WHERE id = %s", (user_id,))
        conn.commit()

    _audit(actor.get("sub"), "admin.users.enable", "users", str(user_id))
    return {"status": "ok"}


@app.get("/v1/admin/roles")
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


@app.post("/v1/admin/roles")
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


@app.patch("/v1/admin/roles/{role_id}")
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


@app.get("/v1/admin/permissions")
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


@app.put("/v1/admin/users/{user_id}/roles")
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


@app.put("/v1/admin/roles/{role_id}/permissions")
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


@app.get("/v1/admin/features")
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


@app.post("/v1/admin/features")
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


@app.patch("/v1/admin/features/{feature_id}")
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


@app.get("/v1/admin/audit-logs")
def admin_audit_logs(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, actor_username, action, target_type, target_id, detail, created_at
                FROM audit_logs
                ORDER BY id DESC
                LIMIT 200
                """
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
        ]
    }


@app.get("/v1/admin/doc-sync/sources")
def admin_doc_sync_sources(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, provider, env_profile, source_name, source_type,
                    external_doc_id, external_sheet_id, source_url, status,
                    last_sync_at, created_at, updated_at,
                    (
                        SELECT COUNT(*)
                        FROM external_records er
                        WHERE er.source_id = external_sources.id
                    ) AS record_count
                FROM external_sources
                ORDER BY updated_at DESC, id DESC
                LIMIT 500
                """
            )
            rows = cur.fetchall()

    return {
        "items": [
            {
                "id": row[0],
                "provider": row[1],
                "env_profile": row[2],
                "source_name": row[3],
                "source_type": row[4],
                "external_doc_id": row[5],
                "external_sheet_id": row[6],
                "source_url": row[7],
                "status": row[8],
                "last_sync_at": str(row[9]) if row[9] else None,
                "created_at": str(row[10]),
                "updated_at": str(row[11]),
                "record_count": row[12],
                "open_url": row[7] or (
                    f"https://doc.weixin.qq.com/smartsheet/{row[5]}?sheet_id={row[6]}"
                    if row[5] and row[6]
                    else (f"https://doc.weixin.qq.com/smartsheet/{row[5]}" if row[5] else "")
                ),
            }
            for row in rows
        ]
    }


@app.get("/v1/admin/doc-sync/runs")
def admin_doc_sync_runs(
    limit: int = Query(default=100, ge=1, le=500),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, provider, env_profile, mode, status, started_at, finished_at,
                    source_count, sheet_count, record_count, created_count, updated_count,
                    error_count, error_json
                FROM sync_runs
                ORDER BY started_at DESC, id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

    return {
        "items": [
            {
                "id": row[0],
                "provider": row[1],
                "env_profile": row[2],
                "mode": row[3],
                "status": row[4],
                "started_at": str(row[5]),
                "finished_at": str(row[6]) if row[6] else None,
                "source_count": row[7],
                "sheet_count": row[8],
                "record_count": row[9],
                "created_count": row[10],
                "updated_count": row[11],
                "error_count": row[12],
                "error_json": row[13],
            }
            for row in rows
        ]
    }


def _doc_sync_records(source_id: int | None, limit: int, offset: int) -> dict[str, Any]:
    where = "WHERE er.source_id = %s" if source_id is not None else ""
    params: list[Any] = []
    if source_id is not None:
        params.append(source_id)
    params.extend([limit, offset])

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    er.id, er.source_id, es.source_name, es.env_profile,
                    er.external_record_id, er.record_hash, er.normalized_json,
                    er.external_created_at, er.external_updated_at, er.synced_at
                FROM external_records er
                JOIN external_sources es ON es.id = er.source_id
                {where}
                ORDER BY er.synced_at DESC, er.id DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()

    return {
        "items": [
            {
                "id": row[0],
                "source_id": row[1],
                "source_name": row[2],
                "env_profile": row[3],
                "external_record_id": row[4],
                "record_hash": row[5],
                "normalized_json": row[6],
                "external_created_at": row[7],
                "external_updated_at": row[8],
                "synced_at": str(row[9]),
            }
            for row in rows
        ]
    }


@app.get("/v1/admin/doc-sync/records")
def admin_doc_sync_records(
    source_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return _doc_sync_records(source_id=source_id, limit=limit, offset=offset)


@app.get("/v1/admin/doc-sync/sources/{source_id}/records")
def admin_doc_sync_source_records(
    source_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return _doc_sync_records(source_id=source_id, limit=limit, offset=offset)


@app.get("/v1/admin/doc-sync/requests")
def admin_doc_sync_requests(
    limit: int = Query(default=100, ge=1, le=500),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    sr.id, sr.source_id, es.source_name, sr.provider, sr.env_profile,
                    sr.mode, sr.status, sr.requested_by, sr.requested_at,
                    sr.started_at, sr.finished_at, sr.sync_run_id, sr.error_json
                FROM sync_requests sr
                JOIN external_sources es ON es.id = sr.source_id
                ORDER BY sr.requested_at DESC, sr.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

    return {
        "items": [
            {
                "id": row[0],
                "source_id": row[1],
                "source_name": row[2],
                "provider": row[3],
                "env_profile": row[4],
                "mode": row[5],
                "status": row[6],
                "requested_by": row[7],
                "requested_at": str(row[8]),
                "started_at": str(row[9]) if row[9] else None,
                "finished_at": str(row[10]) if row[10] else None,
                "sync_run_id": row[11],
                "error_json": row[12],
            }
            for row in rows
        ]
    }


@app.post("/v1/admin/doc-sync/sources/{source_id}/sync-requests")
def admin_create_doc_sync_request(
    source_id: int,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT provider, env_profile
                FROM external_sources
                WHERE id = %s AND status = 'active'
                """,
                (source_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="doc sync source not found")
            cur.execute(
                """
                INSERT INTO sync_requests(source_id, provider, env_profile, mode, status, requested_by)
                VALUES (%s, %s, %s, 'manual', 'pending', %s)
                RETURNING id
                """,
                (source_id, row[0], row[1], actor.get("sub")),
            )
            request_id = cur.fetchone()[0]
        conn.commit()

    _audit(actor.get("sub"), "admin.doc_sync.request", "external_sources", str(source_id), {"request_id": request_id})
    return {"id": request_id, "status": "pending"}


@app.get("/v1/admin/couple-spaces")
def admin_couple_spaces(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, start_date, theme, created_at FROM couple_spaces ORDER BY id DESC")
            spaces = cur.fetchall()
            cur.execute(
                """
                SELECT cm.couple_space_id, u.id, u.username, u.display_name, cm.role, cm.joined_at
                FROM couple_members cm
                JOIN users u ON u.id = cm.user_id
                ORDER BY cm.couple_space_id DESC, cm.id ASC
                """
            )
            member_rows = cur.fetchall()

    members_map: dict[int, list[dict[str, Any]]] = {}
    for row in member_rows:
        members_map.setdefault(row[0], []).append(
            {
                "user_id": row[1],
                "username": row[2],
                "display_name": row[3],
                "role": row[4],
                "joined_at": str(row[5]),
            }
        )

    return {
        "items": [
            {
                "id": row[0],
                "name": row[1],
                "start_date": str(row[2]) if row[2] else None,
                "theme": row[3],
                "created_at": str(row[4]),
                "members": members_map.get(row[0], []),
            }
            for row in spaces
        ]
    }


@app.post("/v1/admin/couple-spaces")
def admin_create_couple_space(
    body: CreateCoupleSpaceRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO couple_spaces(name, start_date, theme) VALUES (%s, %s, %s) RETURNING id",
                (name, body.start_date, body.theme),
            )
            new_id = cur.fetchone()[0]
        conn.commit()

    _audit(actor.get("sub"), "admin.couple_spaces.create", "couple_spaces", str(new_id), {"name": name})
    return {"id": new_id}


@app.post("/v1/admin/couple-spaces/{space_id}/members")
def admin_add_couple_member(
    space_id: int,
    body: AddCoupleMemberRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    role = body.role.strip() or "member"
    if role not in {"member", "owner"}:
        raise HTTPException(status_code=400, detail="invalid role")

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM couple_spaces WHERE id = %s", (space_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="couple space not found")
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            user_row = cur.fetchone()
            if not user_row:
                raise HTTPException(status_code=404, detail="user not found")
            cur.execute(
                """
                INSERT INTO couple_members(couple_space_id, user_id, role)
                VALUES (%s, %s, %s)
                ON CONFLICT(couple_space_id, user_id)
                DO UPDATE SET role = EXCLUDED.role
                """,
                (space_id, user_row[0], role),
            )
        conn.commit()

    _audit(actor.get("sub"), "admin.couple_spaces.add_member", "couple_spaces", str(space_id), {"username": username, "role": role})
    return {"status": "ok"}
