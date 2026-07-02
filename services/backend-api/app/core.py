"""共享基础设施：数据库连接、令牌签发与校验、审计日志、登录/管理员/权限依赖、功能清单与 couple 访问判定。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import psycopg
import time
import uuid

from contextlib import closing
from passlib.context import CryptContext
from psycopg.types.json import Jsonb
from typing import Any

from fastapi import Depends, HTTPException, Header

from app.logging_utils import configure_logging

_request_logger = configure_logging("aliecs.request")


pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

DEFAULT_FEATURES: list[dict[str, Any]] = [
    {"id": 9, "code": "raw_inventory", "title": "原材料库存", "description": "原材料库存查询", "url": "/inventory/raw-materials/", "category": "业务查询", "required_permission": "inventory.raw.read", "status": "active", "sort_order": 10},
    {"id": 10, "code": "finished_inventory", "title": "成品库存", "description": "成品库存查询", "url": "/inventory/finished-goods/", "category": "业务查询", "required_permission": "inventory.finished.read", "status": "active", "sort_order": 20},
    {"id": 7, "code": "formula_query", "title": "系统配方", "description": "配方检索、BOM 同步与成本核算", "url": "/formula/", "category": "业务查询", "required_permission": "formula.read", "status": "active", "sort_order": 30},
    {"id": 1, "code": "new_model_form", "title": "新品型号录入表", "description": "新品型号登记", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_4b7094", "category": "业务录入", "required_permission": "production.schedule.write", "status": "active", "sort_order": 40},
    {"id": 2, "code": "schedule_form", "title": "排产登记表", "description": "排产信息登记", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_e3792e", "category": "业务录入", "required_permission": "production.schedule.write", "status": "active", "sort_order": 50},
    {"id": 3, "code": "pending_return_alert", "title": "待处理+退货提醒", "description": "待处理与退货提醒", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_4501d0", "category": "业务录入", "required_permission": "production.schedule.read", "status": "active", "sort_order": 60},
    {"id": 4, "code": "naming_form", "title": "产品命名登记", "description": "产品命名录入", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_a577fc", "category": "业务录入", "required_permission": "formula.write", "status": "active", "sort_order": 70},
    {"id": 5, "code": "qc_form", "title": "检测数据登记表", "description": "检测数据登记", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_b669cf", "category": "质检", "required_permission": "formula.read", "status": "active", "sort_order": 80},
    {"id": 6, "code": "density_calculator", "title": "配方密度计算器", "description": "配方密度工具", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_bac993", "category": "质检", "required_permission": "formula.read", "status": "active", "sort_order": 90},
    {"id": 8, "code": "midea_requirement", "title": "美的需求", "description": "需求查询入口", "url": None, "category": "业务查询", "required_permission": "midea.requirement.read", "status": "reserved", "sort_order": 100},
    {"id": 11, "code": "personal_section", "title": "个人板块", "description": "个人工具入口", "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_0c521a", "category": "个人", "required_permission": "personal.access", "status": "active", "sort_order": 110},
    {"id": 12, "code": "admin_ui", "title": "Admin UI", "description": "管理后台入口", "url": "/admin/", "category": "系统", "required_permission": "admin.access", "status": "active", "sort_order": 120},
    {"id": 13, "code": "ash_calculator", "title": "分段灰分/填料计算器", "description": "分段灼烧灰分与炭黑/填料半定量计算，支持导出PDF报告", "url": "/tools/ash-calc/", "category": "工具", "required_permission": None, "status": "active", "sort_order": 130},
    {"id": 14, "code": "ai_file_transfer", "title": "AI 文件中转", "description": "上传临时文件，生成公开下载链接。", "url": "https://files.hydwang.xyz", "category": "工具", "required_permission": None, "status": "active", "sort_order": 140},
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
    if env_name == "prod" and len(secret) < 32:
        raise HTTPException(status_code=500, detail="AUTH_TOKEN_SECRET must be at least 32 characters in production")
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
    payload = dict(payload)
    payload.setdefault("jti", uuid.uuid4().hex)
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


def _current_token_version(user_id: int) -> int | None:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT token_version FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    return int(row[0])


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
    payload = _decode_token(token)
    uid = payload.get("uid")
    if uid is not None and "tv" in payload:
        current = _current_token_version(int(uid))
        if current is not None and int(payload["tv"]) != current:
            raise HTTPException(status_code=401, detail="token revoked")
    return payload


def require_login(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return user


def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    roles = user.get("roles", [])
    permissions = user.get("permissions", [])
    if "admin" in roles or "admin.access" in permissions:
        return user
    raise HTTPException(status_code=403, detail="permission denied")


def require_permission(permission: str, user: dict[str, Any]) -> dict[str, Any]:
    roles = user.get("roles", [])
    permissions = user.get("permissions", [])
    if "admin" in roles or "admin.access" in permissions or permission in permissions:
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
