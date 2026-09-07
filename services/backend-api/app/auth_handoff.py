"""Shared, one-use browser-bound login handoffs; never put a session token in a URL."""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import re
import secrets
import time

HANDOFF_TTL_SECONDS = 60
MARKET_ORIGIN = "https://market.hydwang.xyz"


def valid_challenge(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", value))


class HandoffStore:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def issue(self, *, token: str, challenge: str, origin: str, now: float | None = None) -> str:
        if not valid_challenge(challenge) or origin != MARKET_ORIGIN:
            raise ValueError("invalid browser binding or return origin")
        now = time.time() if now is None else now
        code = secrets.token_urlsafe(32)
        with closing(self._connect()) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM auth_browser_handoffs WHERE expires_at <= %s", (now,))
                cur.execute(
                    "INSERT INTO auth_browser_handoffs (code_hash, challenge, origin, session_token, expires_at) VALUES (%s, %s, %s, %s, %s)",
                    (hashlib.sha256(code.encode()).hexdigest(), challenge, origin, token, now + HANDOFF_TTL_SECONDS),
                )
            conn.commit()
        return code

    def consume(self, *, code: str, verifier: str, origin: str, now: float | None = None) -> str | None:
        if origin != MARKET_ORIGIN or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
            return None
        now = time.time() if now is None else now
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        # One DELETE transaction, not SELECT then DELETE: only one worker can consume.
        with closing(self._connect()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM auth_browser_handoffs WHERE code_hash = %s AND challenge = %s AND origin = %s AND expires_at > %s RETURNING session_token",
                    (hashlib.sha256(code.encode()).hexdigest(), challenge, origin, now),
                )
                row = cur.fetchone()
            conn.commit()
        return row[0] if row else None
