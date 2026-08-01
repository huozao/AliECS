from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi import HTTPException


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core import require_admin, require_permission  # noqa: E402


class PermissionMessageTests(unittest.TestCase):
    def test_permission_denials_are_generic_but_keep_403(self) -> None:
        for check in (
            lambda: require_admin({"roles": [], "permissions": []}),
            lambda: require_permission("formula.cost.calculate", {"roles": [], "permissions": []}),
        ):
            with self.subTest(check=check):
                with self.assertRaises(HTTPException) as ctx:
                    check()
                self.assertEqual(403, ctx.exception.status_code)
                self.assertEqual("当前功能不可用。", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
