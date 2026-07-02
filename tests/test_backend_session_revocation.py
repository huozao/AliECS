from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_main():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import core as main

    return main


class TokenRevocationTests(unittest.TestCase):
    def test_encode_token_includes_jti_and_tv(self) -> None:
        main = load_main()
        with patch.dict("os.environ", {"AUTH_TOKEN_SECRET": "x" * 32, "ENV": "dev"}):
            payload = {
                "sub": "alice",
                "uid": 1,
                "roles": [],
                "permissions": [],
                "tv": 1,
                "iat": 0,
                "exp": 9999999999,
            }
            token = main._encode_token(payload)
            decoded = main._decode_token(token)

        self.assertIn("jti", decoded)
        self.assertEqual(decoded["tv"], 1)

    def test_decode_token_with_stale_token_version_is_rejected(self) -> None:
        main = load_main()
        with patch.dict("os.environ", {"AUTH_TOKEN_SECRET": "x" * 32, "ENV": "dev"}):
            payload = {
                "sub": "alice",
                "uid": 1,
                "roles": [],
                "permissions": [],
                "tv": 1,
                "iat": 0,
                "exp": 9999999999,
                "jti": "abc",
            }
            token = main._encode_token(payload)

            with patch.object(main, "_current_token_version", return_value=2):
                with self.assertRaises(main.HTTPException) as ctx:
                    main.get_current_user(authorization=f"Bearer {token}")

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("revoked", ctx.exception.detail)

    def test_token_secret_rejects_short_secret_in_prod(self) -> None:
        main = load_main()
        with patch.dict("os.environ", {"AUTH_TOKEN_SECRET": "short", "ENV": "prod"}, clear=False):
            with self.assertRaises(main.HTTPException) as ctx:
                main._token_secret()

        self.assertEqual(ctx.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
