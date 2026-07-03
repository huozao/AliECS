"""OIDC 登录域：对接 Authelia（授权码 + PKCE），成功后按 username 首绑/按 oidc_sub 复认，换发本站 HMAC 会话 token。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
import uuid

from contextlib import closing
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core import _audit, _conn, _encode_token, _token_ttl_seconds, _user_roles_permissions

router = APIRouter()

_STATE_TTL_SECONDS = 600
# state -> (code_verifier, created_at)。进程内存态：现网单 uvicorn worker 成立；
# 若将来扩多 worker/多实例，必须改成 DB/共享存储，否则回调会随机 400。
_pending_states: dict[str, tuple[str, float]] = {}
_discovery_cache: dict[str, dict[str, Any]] = {}


def _oidc_enabled() -> bool:
    return os.getenv("OIDC_ENABLED", "false").strip().lower() == "true"


def _oidc_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=500, detail=f"{name} not configured")
    return value


def _http_get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_post_form(url: str, data: dict[str, str], headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    merged = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    request = urllib.request.Request(url, data=body, headers=merged)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _discovery() -> dict[str, Any]:
    issuer = _oidc_env("OIDC_ISSUER").rstrip("/")
    cached = _discovery_cache.get(issuer)
    if cached:
        return cached
    doc = _http_get_json(f"{issuer}/.well-known/openid-configuration")
    _discovery_cache[issuer] = doc
    return doc


def _prune_states(now: float) -> None:
    expired = [key for key, (_, created) in _pending_states.items() if now - created > _STATE_TTL_SECONDS]
    for key in expired:
        _pending_states.pop(key, None)


@router.get("/v1/auth/oidc/login")
def oidc_login() -> RedirectResponse:
    if not _oidc_enabled():
        raise HTTPException(status_code=404, detail="oidc disabled")
    now = time.time()
    _prune_states(now)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    _pending_states[state] = (verifier, now)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    params = {
        "response_type": "code",
        "client_id": _oidc_env("OIDC_CLIENT_ID"),
        "redirect_uri": _oidc_env("OIDC_REDIRECT_URI"),
        "scope": "openid profile groups",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(
        f"{_discovery()['authorization_endpoint']}?{urllib.parse.urlencode(params)}", status_code=302
    )


@router.get("/v1/auth/oidc/callback")
def oidc_callback(code: str = "", state: str = "") -> HTMLResponse:
    if not _oidc_enabled():
        raise HTTPException(status_code=404, detail="oidc disabled")
    entry = _pending_states.pop(state, None) if state else None
    if not code or entry is None or time.time() - entry[1] > _STATE_TTL_SECONDS:
        raise HTTPException(status_code=400, detail="invalid state")

    doc = _discovery()
    client_id = _oidc_env("OIDC_CLIENT_ID")
    basic = base64.b64encode(f"{client_id}:{_oidc_env('OIDC_CLIENT_SECRET')}".encode("utf-8")).decode("ascii")
    token_doc = _http_post_form(
        doc["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _oidc_env("OIDC_REDIRECT_URI"),
            "code_verifier": entry[0],
        },
        headers={"Authorization": f"Basic {basic}"},
    )
    access_token = str(token_doc.get("access_token", ""))
    if not access_token:
        raise HTTPException(status_code=502, detail="token exchange failed")

    userinfo = _http_get_json(doc["userinfo_endpoint"], headers={"Authorization": f"Bearer {access_token}"})
    sub = str(userinfo.get("sub", "")).strip()
    preferred = str(userinfo.get("preferred_username", "")).strip()
    if not sub:
        raise HTTPException(status_code=502, detail="userinfo missing sub")

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, display_name, status, is_admin, token_version FROM users WHERE oidc_sub = %s",
                (sub,),
            )
            row = cur.fetchone()
            if row is None and preferred:
                # 首次登录：按 username 绑定（lldap uid 与网站 username 一致建号）；绑定后只认 oidc_sub。
                cur.execute(
                    """
                    UPDATE users SET oidc_sub = %s, updated_at = NOW()
                    WHERE username = %s AND oidc_sub IS NULL
                    RETURNING id, username, display_name, status, is_admin, token_version
                    """,
                    (sub, preferred),
                )
                row = cur.fetchone()
            if row is None or row[3] != "active":
                conn.rollback()
                raise HTTPException(status_code=403, detail="account not provisioned")

            user_id = int(row[0])
            roles, permissions = _user_roles_permissions(user_id, bool(row[4]))
            now_ts = int(time.time())
            payload = {
                "sub": row[1],
                "uid": user_id,
                "display_name": row[2],
                "roles": roles,
                "permissions": permissions,
                "tv": int(row[5]),
                "jti": uuid.uuid4().hex,
                "iat": now_ts,
                "exp": now_ts + _token_ttl_seconds(),
            }
            cur.execute("UPDATE users SET last_login_at = NOW(), updated_at = NOW() WHERE id = %s", (user_id,))
        conn.commit()

    _audit(row[1], "auth.oidc.login")
    token_js = json.dumps(_encode_token(payload))
    html = (
        '<!doctype html><meta charset="utf-8"><title>登录成功</title>'
        "<script>var token=" + token_js + ";"
        '["aliecs_auth_token","portal_token","admin_token"].forEach(function(key){localStorage.setItem(key,token);});'
        'location.replace("/");</script>登录成功，正在跳转……'
    )
    return HTMLResponse(html)
