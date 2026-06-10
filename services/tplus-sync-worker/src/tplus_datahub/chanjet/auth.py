from __future__ import annotations

import os
from typing import Any

from config.settings import Settings


def resolve_open_token(settings: Settings) -> str:
    """Prefer the token file refreshed by backend-api (appTicket auto exchange); fall back to env token.

    Read fresh on every call so a refreshed token takes effect without restarting the worker.
    """
    path = os.getenv("CHANJET_OPEN_TOKEN_FILE", "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
            if token:
                return token
        except OSError:
            pass
    return settings.open_token


def build_auth_headers(settings: Settings) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "appKey": settings.app_key,
        "appSecret": settings.app_secret,
        "openToken": resolve_open_token(settings),
    }
