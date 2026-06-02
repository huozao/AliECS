from __future__ import annotations

from typing import Any

from config.settings import Settings


def build_auth_headers(settings: Settings) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "appKey": settings.app_key,
        "appSecret": settings.app_secret,
        "openToken": settings.open_token,
    }
