from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class FormulaSharedTokenDisabledTests(unittest.TestCase):
    """人工配方接口只接受可追溯到 SSO 用户的 Bearer Token。"""

    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        self._tmp = tempfile.TemporaryDirectory()
        source = Path(self._tmp.name) / "bom.xlsx"
        with pd.ExcelWriter(source, engine="openpyxl") as writer:
            pd.DataFrame([{
                "父件编码": "CP001", "父件名称": "测试产品", "规格型号": "PP", "版本号": "V1",
                "计量单位": "kg", "生产数量": 25, "默认BOM": 1, "停用": 0,
            }]).to_excel(writer, sheet_name="物料清单", index=False)
            pd.DataFrame([{
                "版本号": "V1", "父件编码": "CP001", "子件编码": "M001", "子件名称": "树脂",
                "规格型号": "A", "计量单位": "kg", "需用数量": 2, "系统单价": 10,
            }]).to_excel(writer, sheet_name="子件明细", index=False)
        os.environ["AUTH_TOKEN_SECRET"] = "test-user-token-secret"
        os.environ["RECIPE_BOM_INPUT_PATH"] = str(source)
        os.environ["RECIPE_EXPORT_DIR"] = str(Path(self._tmp.name) / "exports")
        from app.core import _encode_token
        from app.main import app
        self._encode_token = _encode_token
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = self._old_sys_path

    def test_x_api_key_no_longer_authenticates_formula_query(self) -> None:
        response = self.client.post(
            "/v1/recipes/query", json={"query": "CP001"}, headers={"X-API-Key": "legacy-shared-token"}
        )
        self.assertEqual(401, response.status_code)

    def test_bound_user_bearer_token_still_works(self) -> None:
        token = self._encode_token({
            "uid": 7, "sub": "alice", "username": "alice", "auth_source": "miniapp",
            "roles": [], "permissions": ["formula.read"], "exp": int(time.time()) + 3600,
        })
        response = self.client.post(
            "/v1/recipes/query", json={"query": "CP001"},
            headers={"Authorization": f"Bearer {token}", "X-Client-Channel": "miniapp"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()["recipe_count"])


if __name__ == "__main__":
    unittest.main()
