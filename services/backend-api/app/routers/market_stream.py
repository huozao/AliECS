"""Issue short-lived, read-only Centrifugo market connection tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Response

from app.core import _user_roles_permissions, require_login

router = APIRouter(prefix="/v1/market")
_CHANNEL = "gold:market"
_AUDIENCE = "gold-market-stream"
_ISSUER = "aliecs-market"


def _segment(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":"),
                                    sort_keys=True).encode()).rstrip(b"=").decode()


def require_stream_reader(user: dict = Depends(require_login)) -> dict:
    uid = user.get("uid", user.get("id"))
    if type(uid) is not int or uid <= 0:
        raise HTTPException(401, "invalid authenticated user identity")
    roles, permissions = _user_roles_permissions(uid, bool(user.get("is_admin")))
    if ("admin" not in roles and "admin.access" not in permissions
            and "market.read" not in permissions):
        raise HTTPException(403, "当前功能不可用。")
    return user


@router.get("/stream-token")
def market_stream_token(response: Response, user: dict = Depends(require_stream_reader)) -> dict:
    uid = user.get("uid", user.get("id"))
    secret = os.getenv("GSM_CENTRIFUGO_TOKEN_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "market stream authentication unavailable")
    now = int(time.time())
    header = _segment({"alg": "HS256", "typ": "JWT"})
    claims = _segment({"sub": str(uid), "iss": _ISSUER, "aud": _AUDIENCE,
                       "iat": now, "exp": now + 60, "channels": [_CHANNEL]})
    signed = f"{header}.{claims}"
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), signed.encode(),
                                                  hashlib.sha256).digest()).rstrip(b"=").decode()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {"token": f"{signed}.{signature}", "expires_at": now + 60,
            "channel": _CHANNEL, "transport": "centrifugo"}
