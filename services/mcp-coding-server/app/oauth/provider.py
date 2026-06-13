from __future__ import annotations

import json
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import OAuthConfig
from .store import OAuthStore


class AliecsOAuthProvider(OAuthAuthorizationServerProvider):
    """Self-hosted OAuth provider for ChatGPT MCP connector calls."""

    def __init__(self, config: OAuthConfig, store: OAuthStore) -> None:
        self.config = config
        self.store = store

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("client_id is required")
        self.store.put_client(client_info.client_id, client_info.model_dump_json())

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if not client.client_id:
            raise ValueError("client_id is required")
        txn = secrets.token_urlsafe(24)
        self.store.put_pending(
            txn, client.client_id, params.model_dump_json(), self.config.txn_ttl
        )
        return f"{self.config.issuer_url}/oauth/consent?txn={txn}"

    def complete_authorization(self, client_id: str, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(32)
        scopes = list(params.scopes or [self.config.scope])
        now = time.time()
        meta = {
            "scopes": scopes,
            "client_id": client_id,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": str(params.resource) if params.resource else None,
            "expires_at": now + self.config.code_ttl,
        }
        self.store.put_hashed(
            "auth_codes", code, json.dumps(meta), self.config.code_ttl
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self.store.get_hashed("auth_codes", authorization_code)
        if not data:
            return None
        meta = json.loads(data)
        if meta["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=meta["scopes"],
            expires_at=float(meta["expires_at"]),
            client_id=meta["client_id"],
            code_challenge=meta["code_challenge"],
            redirect_uri=meta["redirect_uri"],
            redirect_uri_provided_explicitly=meta["redirect_uri_provided_explicitly"],
            resource=meta.get("resource"),
            subject=None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.store.delete_hashed("auth_codes", authorization_code.code)
        return self._issue(
            authorization_code.client_id,
            list(authorization_code.scopes),
            authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self.store.get_hashed("refresh_tokens", refresh_token)
        if not data:
            return None
        meta = json.loads(data)
        if meta["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=meta["client_id"],
            scopes=meta["scopes"],
            expires_at=meta.get("expires_at"),
            subject=None,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self.store.delete_hashed("refresh_tokens", refresh_token.token)
        next_scopes = list(scopes or refresh_token.scopes)
        if not set(next_scopes).issubset(set(refresh_token.scopes)):
            raise ValueError("requested scopes exceed refresh token scope")
        return self._issue(refresh_token.client_id, next_scopes, None)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.store.get_hashed("access_tokens", token)
        if not data:
            return None
        meta = json.loads(data)
        return AccessToken(
            token=token,
            client_id=meta["client_id"],
            scopes=meta["scopes"],
            expires_at=meta.get("expires_at"),
            resource=meta.get("resource"),
            subject=None,
            claims=None,
        )

    async def revoke_token(self, token) -> None:
        raw = getattr(token, "token", None)
        if raw:
            self.store.delete_hashed("access_tokens", raw)
            self.store.delete_hashed("refresh_tokens", raw)

    def _issue(self, client_id: str, scopes: list[str], resource) -> OAuthToken:
        now = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        res = str(resource) if resource else None
        self.store.put_hashed(
            "access_tokens",
            access,
            json.dumps(
                {
                    "client_id": client_id,
                    "scopes": scopes,
                    "expires_at": int(now + self.config.access_ttl),
                    "resource": res,
                }
            ),
            self.config.access_ttl,
        )
        self.store.put_hashed(
            "refresh_tokens",
            refresh,
            json.dumps(
                {
                    "client_id": client_id,
                    "scopes": scopes,
                    "expires_at": int(now + self.config.refresh_ttl),
                }
            ),
            self.config.refresh_ttl,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.config.access_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )
