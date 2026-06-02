from __future__ import annotations

import json
import os
from typing import Any
from urllib import parse, request


DEFAULT_OPENAPI_BASE_URL = "https://openapi.chanjet.com"


def get_app_access_token(app_ticket: str) -> dict[str, Any]:
    return _openapi_post(
        "/auth/appAuth/getAppAccessToken",
        {"appTicket": app_ticket},
    )


def get_permanent_auth_code(app_access_token: str, temp_auth_code: str) -> dict[str, Any]:
    return _openapi_post(
        "/auth/orgAuth/getPermanentAuthCode",
        {
            "appAccessToken": app_access_token,
            "tempAuthCode": temp_auth_code,
        },
    )


def get_org_access_token(app_access_token: str, permanent_auth_code: str) -> dict[str, Any]:
    return _openapi_post(
        "/auth/orgAuth/getOrgAccessToken",
        {
            "appAccessToken": app_access_token,
            "permanentAuthCode": permanent_auth_code,
        },
    )


def exchange_authorization_code(code: str, redirect_uri: str) -> dict[str, Any]:
    query = {
        "grantType": "authorization_code",
        "redirectUri": redirect_uri,
        "code": code,
    }
    return _openapi_get("/auth/v2/getToken", query)


def get_token_by_user_permanent_code(
    org_access_token: str,
    user_auth_permanent_code: str,
) -> dict[str, Any]:
    return _openapi_post(
        "/auth/token/getTokenByPermanentCode",
        {
            "orgAccessToken": org_access_token,
            "userAuthPermanentCode": user_auth_permanent_code,
        },
    )


def refresh_open_token(refresh_token: str) -> dict[str, Any]:
    query = {
        "grantType": "refresh_token",
        "refreshToken": refresh_token,
    }
    return _openapi_get("/auth/v2/refreshToken", query)


def trigger_app_ticket_push() -> dict[str, Any]:
    return _openapi_post("/auth/appTicket/resend", None)


def trigger_org_temp_auth_code_push(org_id: str, app_name: str, state: str | None = None) -> dict[str, Any]:
    query = {
        "orgId": org_id,
        "appName": app_name,
    }
    if state:
        query["state"] = state
    return _openapi_get("/auth/orgTempAuthCode/resend", query)


def generate_self_built_open_token(app_ticket: str, certificate: str) -> dict[str, Any]:
    return _openapi_post(
        "/v1/common/auth/selfBuiltApp/generateToken",
        {
            "appTicket": app_ticket,
            "certificate": certificate,
        },
    )


def _openapi_get(path: str, query: dict[str, str]) -> dict[str, Any]:
    url = f"{_base_url()}{path}?{parse.urlencode(query)}"
    req = request.Request(url, headers=_headers(), method="GET")
    return _send(req)


def _openapi_post(path: str, body: dict[str, Any] | None) -> dict[str, Any]:
    payload = b""
    if body is not None:
        payload = json.dumps(body, ensure_ascii=True).encode("utf-8")
    req = request.Request(
        f"{_base_url()}{path}",
        data=payload,
        headers=_headers(),
        method="POST",
    )
    return _send(req)


def _send(req: request.Request) -> dict[str, Any]:
    with request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
    if not body:
        return {}
    data = json.loads(body)
    if isinstance(data, dict):
        return data
    return {"value": data}


def _base_url() -> str:
    return os.getenv("CHANJET_OPENAPI_BASE_URL", DEFAULT_OPENAPI_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    app_key = _secret_value("CHANJET_APP_KEY")
    app_secret = _secret_value("CHANJET_APP_SECRET")
    if not app_key or not app_secret:
        raise RuntimeError("CHANJET_APP_KEY and CHANJET_APP_SECRET are required")
    return {
        "appKey": app_key,
        "appSecret": app_secret,
        "Content-Type": "application/json",
    }


def _secret_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value

    file_path = os.getenv(f"{name}_FILE", "").strip()
    candidates = [file_path] if file_path else []
    candidates.extend(
        [
            f"/run/secrets/{name.lower()}",
            f"/tmp/{name.lower()}",
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            continue
    return ""
