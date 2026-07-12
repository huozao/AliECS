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


class FormulaApiTokenTests(unittest.TestCase):
    """FORMULA_API_TOKEN 只读通道：匹配放行只读路由 / 不匹配401 / env缺失=通道关闭 / 写路由不放行。"""

    TOKEN = "test-formula-token-abc123"

    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        self._old_env = {
            name: os.environ.get(name)
            for name in (
                "AUTH_TOKEN_SECRET",
                "RECIPE_BOM_INPUT_PATH",
                "RECIPE_EXPORT_DIR",
                "TPLUS_BOM_SYNC_REQUEST_DIR",
                "FORMULA_API_TOKEN",
            )
        }
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]
        sys.path.insert(0, backend_root)

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.source_path = self._write_source_workbook()
        os.environ["AUTH_TOKEN_SECRET"] = "test-formula-token-secret"
        os.environ["RECIPE_BOM_INPUT_PATH"] = str(self.source_path)
        os.environ["RECIPE_EXPORT_DIR"] = str(self.tmp_path / "exports")
        os.environ["TPLUS_BOM_SYNC_REQUEST_DIR"] = str(self.tmp_path / "tplus-sync-requests")
        os.environ["FORMULA_API_TOKEN"] = self.TOKEN

        from app.core import _encode_token
        from app.main import app

        self._encode_token = _encode_token
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = self._old_sys_path

    def _write_source_workbook(self) -> Path:
        source = self.tmp_path / "bom_20260712.xlsx"
        material_rows = [
            {
                "父件编码": "30122027-3027",
                "父件名称": "BX3027-海尔洗衣机PP海灰色母",
                "规格型号": "PP",
                "版本号": "V1",
                "计量单位": "kg",
                "生产数量": 25,
                "默认BOM": 1,
                "停用": 0,
            }
        ]
        child_rows = [
            {
                "版本号": "V1",
                "父件编码": "30122027-3027",
                "子件编码": "C001",
                "子件名称": "树脂",
                "规格型号": "A",
                "计量单位": "kg",
                "需用数量": 2,
                "系统单价": 10,
            }
        ]
        with pd.ExcelWriter(source, engine="openpyxl") as writer:
            pd.DataFrame(material_rows).to_excel(writer, sheet_name="物料清单", index=False)
            pd.DataFrame(child_rows).to_excel(writer, sheet_name="子件明细", index=False)
        return source

    def _query_body(self) -> dict:
        return {"query": "30122027-3027", "default_bom": "all", "include_disabled": True}

    def test_matching_token_allows_readonly_routes(self) -> None:
        headers = {"X-API-Key": self.TOKEN}
        resp = self.client.post("/v1/recipes/query", json=self._query_body(), headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(resp.json()["match_count"], 0)

        resp = self.client.post("/v1/recipes/cost", json=self._query_body(), headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["recipe_count"], 1)

        resp = self.client.post("/v1/recipes/cost/export", json=self._query_body(), headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp.headers["content-type"])

        # compare/export：rows 允许为空，只验证鉴权门打开（200 而非 401）
        compare_body = {"query": "x", "versions": [{"label": "V1"}], "rows": []}
        resp = self.client.post("/v1/recipes/compare/export", json=compare_body, headers=headers)
        self.assertEqual(resp.status_code, 200)

    def test_download_with_token(self) -> None:
        headers = {"X-API-Key": self.TOKEN}
        file_id = self.client.post("/v1/recipes/query", json=self._query_body(), headers=headers).json()["file_id"]
        resp = self.client.get(f"/v1/recipes/download/{file_id}", headers=headers)
        self.assertEqual(resp.status_code, 200)

    def test_wrong_token_rejected(self) -> None:
        resp = self.client.post(
            "/v1/recipes/query", json=self._query_body(), headers={"X-API-Key": "wrong-token"}
        )
        self.assertEqual(resp.status_code, 401)

    def test_channel_closed_without_env(self) -> None:
        os.environ.pop("FORMULA_API_TOKEN", None)
        resp = self.client.post(
            "/v1/recipes/query", json=self._query_body(), headers={"X-API-Key": self.TOKEN}
        )
        self.assertEqual(resp.status_code, 401)

    def test_write_route_not_covered(self) -> None:
        resp = self.client.post("/v1/recipes/sync-bom", headers={"X-API-Key": self.TOKEN})
        self.assertEqual(resp.status_code, 401)

    def test_invalid_key_falls_back_to_bearer(self) -> None:
        token = self._encode_token(
            {"sub": "u", "roles": [], "permissions": ["formula.read"], "exp": int(time.time()) + 3600}
        )
        resp = self.client.post(
            "/v1/recipes/query",
            json=self._query_body(),
            headers={"X-API-Key": "wrong-token", "Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
