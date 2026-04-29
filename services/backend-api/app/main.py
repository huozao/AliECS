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
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel

app = FastAPI(title="backend-api-phase1", version="0.2.0")


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "")


def _db_ping() -> tuple[bool, str]:
    database_url = _database_url()
    if not database_url:
        return False, "DATABASE_URL is empty"

    try:
        with closing(psycopg.connect(database_url, connect_timeout=3)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, "db ok"
    except Exception as exc:  # pragma: no cover - defensive path
        return False, f"db error: {exc}"


def _token_secret() -> str:
    return os.getenv("AUTH_TOKEN_SECRET", "change-this-in-production")


def _token_ttl_seconds() -> int:
    return int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "28800"))


def _users() -> dict[str, dict[str, Any]]:
    raw = os.getenv("AUTH_USERS_JSON", "")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # default for bootstrap/demo only
    return {
        "admin": {"password": "admin123", "role": "admin", "permissions": ["personal"]},
        "operator": {"password": "operator123", "role": "operator", "permissions": []},
    }


def _sign(payload: str) -> str:
    mac = hmac.new(_token_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return mac


def _encode_token(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    body_b64 = base64.urlsafe_b64encode(body.encode("utf-8")).decode("utf-8").rstrip("=")
    signature = _sign(body_b64)
    return f"{body_b64}.{signature}"


def _decode_token(token: str) -> dict[str, Any]:
    try:
        body_b64, signature = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token format") from exc

    expected = _sign(body_b64)
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token signature")

    padded = body_b64 + "=" * (-len(body_b64) % 4)
    body = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
    payload = json.loads(body)

    exp = int(payload.get("exp", 0))
    if exp <= int(time.time()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token expired")

    return payload


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing authorization header")

    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid authorization header")

    return authorization[len(prefix) :].strip()


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
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "backend-api",
        "database": {
            "ok": db_ok,
            "message": db_message,
        },
    }


@app.get("/readyz")
def readyz() -> dict[str, object]:
    db_ok, db_message = _db_ping()
    if not db_ok:
        return {
            "status": "not-ready",
            "reason": db_message,
        }

    return {"status": "ready"}


@app.get("/v1/ping")
def ping() -> dict[str, str]:
    return {"message": "pong"}


@app.post("/v1/auth/login")
def auth_login(body: LoginRequest) -> dict[str, Any]:
    users = _users()
    user = users.get(body.username)
    if not user or user.get("password") != body.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid username or password")

    now = int(time.time())
    payload = {
        "sub": body.username,
        "role": user.get("role", "operator"),
        "permissions": user.get("permissions", []),
        "iat": now,
        "exp": now + _token_ttl_seconds(),
    }
    token = _encode_token(payload)

    return {
        "token": token,
        "expires_in": _token_ttl_seconds(),
        "user": {
            "username": body.username,
            "role": payload["role"],
            "permissions": payload["permissions"],
        },
    }


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
def auth_me(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _extract_bearer(authorization)
    payload = _decode_token(token)
    return {
        "username": payload.get("sub", ""),
        "role": payload.get("role", "operator"),
        "permissions": payload.get("permissions", []),
        "exp": payload.get("exp"),
    }


@app.get("/v1/features")
def features(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    user: dict[str, Any] = {"role": "guest", "permissions": []}

    if authorization:
        token = _extract_bearer(authorization)
        payload = _decode_token(token)
        user = {
            "role": payload.get("role", "operator"),
            "permissions": payload.get("permissions", []),
            "username": payload.get("sub", ""),
        }

    has_personal = "personal" in user.get("permissions", []) or user.get("role") == "admin"

    links = {
        "new_model_form": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_4b7094",
        "schedule_form": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_e3792e",
        "pending_return_alert": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_4501d0",
        "naming_form": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_a577fc",
        "qc_form": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_b669cf",
        "density_calculator": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_bac993",
    }

    personal = {
        "enabled": has_personal,
        "title": "人体周期",
        "url": "https://doc.weixin.qq.com/smartsheet/form/1_wp7hSPEQAAT1c_JcnLpU1STlUJOXWRPA_0c521a",
    }

    return {
        "user": user,
        "links": links,
        "personal": personal,
    }
