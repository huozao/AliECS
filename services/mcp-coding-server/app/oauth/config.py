from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class OAuthConfig:
    enabled: bool
    issuer_url: str
    passphrase: str
    pepper: str
    store_path: str
    scope: str = "coding"
    access_ttl: int = 3600
    refresh_ttl: int = 30 * 24 * 3600
    code_ttl: int = 600
    txn_ttl: int = 600

    @property
    def fully_configured(self) -> bool:
        return bool(self.issuer_url and self.passphrase and self.pepper and self.store_path)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def config_from_env() -> OAuthConfig:
    enabled = os.getenv("MCP_OAUTH_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    return OAuthConfig(
        enabled=enabled,
        issuer_url=os.getenv("MCP_OAUTH_ISSUER", "").strip().rstrip("/"),
        passphrase=os.getenv("MCP_OAUTH_PASSPHRASE", ""),
        pepper=os.getenv("MCP_OAUTH_SIGNING_SECRET", ""),
        store_path=os.getenv("MCP_OAUTH_STORE_PATH", "/data/oauth/oauth.db").strip(),
        access_ttl=_int_env("MCP_OAUTH_ACCESS_TTL", 3600),
        refresh_ttl=_int_env("MCP_OAUTH_REFRESH_TTL", 30 * 24 * 3600),
        code_ttl=_int_env("MCP_OAUTH_CODE_TTL", 600),
    )
