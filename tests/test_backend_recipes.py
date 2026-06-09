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


class BackendRecipeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        self._old_env = {
            "AUTH_TOKEN_SECRET": os.environ.get("AUTH_TOKEN_SECRET"),
            "RECIPE_BOM_INPUT_PATH": os.environ.get("RECIPE_BOM_INPUT_PATH"),
            "RECIPE_EXPORT_DIR": os.environ.get("RECIPE_EXPORT_DIR"),
            "TPLUS_BOM_SYNC_REQUEST_DIR": os.environ.get("TPLUS_BOM_SYNC_REQUEST_DIR"),
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
        os.environ["AUTH_TOKEN_SECRET"] = "test-recipe-secret"
        os.environ["RECIPE_BOM_INPUT_PATH"] = str(self.source_path)
        os.environ["RECIPE_EXPORT_DIR"] = str(self.tmp_path / "exports")
        os.environ["TPLUS_BOM_SYNC_REQUEST_DIR"] = str(self.tmp_path / "tplus-sync-requests")

        from app.main import _encode_token, app

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
        source = self.tmp_path / "bom_20260604.xlsx"
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
            },
            {
                "父件编码": "30122027-3027",
                "父件名称": "BX3027-停用旧版",
                "规格型号": "PP",
                "版本号": "V0",
                "计量单位": "kg",
                "生产数量": 25,
                "默认BOM": 0,
                "停用": 1,
            },
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
            },
            {
                "版本号": "V1",
                "父件编码": "30122027-3027",
                "子件编码": "C002",
                "子件名称": "色粉",
                "规格型号": "B",
                "计量单位": "g",
                "需用数量": 500,
                "系统单价": 20,
            },
            {
                "版本号": "V0",
                "父件编码": "30122027-3027",
                "子件编码": "C003",
                "子件名称": "旧版树脂",
                "规格型号": "C",
                "计量单位": "kg",
                "需用数量": 1,
                "系统单价": 30,
            },
        ]
        with pd.ExcelWriter(source, engine="openpyxl") as writer:
            pd.DataFrame(material_rows).to_excel(writer, sheet_name="物料清单", index=False)
            pd.DataFrame(child_rows).to_excel(writer, sheet_name="子件明细", index=False)
        return source

    def _token(self, *, roles: list[str] | None = None, permissions: list[str] | None = None) -> str:
        return self._encode_token(
            {
                "sub": "recipe-user",
                "roles": roles or [],
                "permissions": permissions or [],
                "exp": int(time.time()) + 3600,
            }
        )

    def test_formula_read_user_can_query_recipe_and_download_workbook(self) -> None:
        token = self._token(permissions=["formula.read"])

        response = self.client.post(
            "/v1/recipes/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "3027"},
        )

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(3, data["match_count"])
        self.assertEqual(2, data["recipe_count"])
        self.assertIn("/v1/recipes/download/", data["download_url"])
        self.assertTrue(data["preview"])

        download = self.client.get(data["download_url"], headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(200, download.status_code)
        self.assertEqual(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            download.headers["content-type"],
        )
        self.assertGreater(len(download.content), 0)

    def test_admin_user_can_query_recipe(self) -> None:
        token = self._token(roles=["admin"], permissions=["admin.access"])

        response = self.client.post(
            "/v1/recipes/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "3027", "default_bom": "1"},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, response.json()["match_count"])

    def test_query_requires_bearer_token(self) -> None:
        response = self.client.post("/v1/recipes/query", json={"query": "3027"})

        self.assertIn(response.status_code, {401, 403})

    def test_query_missing_source_returns_404_without_local_path(self) -> None:
        missing_path = self.tmp_path / "missing" / "bom.xlsx"
        os.environ["RECIPE_BOM_INPUT_PATH"] = str(missing_path)
        token = self._token(permissions=["formula.read"])

        response = self.client.post(
            "/v1/recipes/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "3027"},
        )

        self.assertEqual(404, response.status_code)
        detail = response.json()["detail"]
        self.assertEqual("BOM 输入文件未找到", detail)
        self.assertNotIn(str(missing_path), detail)

    def test_download_requires_bearer_token(self) -> None:
        response = self.client.get("/v1/recipes/download/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

        self.assertIn(response.status_code, {401, 403})

    def test_download_requires_formula_read_permission(self) -> None:
        token = self._token(permissions=["production.schedule.read"])

        response = self.client.get(
            "/v1/recipes/download/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(403, response.status_code)

    def test_download_rejects_invalid_file_id(self) -> None:
        token = self._token(permissions=["formula.read"])

        response = self.client.get(
            "/v1/recipes/download/not-a-valid-file-id",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(400, response.status_code)

    def test_download_returns_404_for_missing_file(self) -> None:
        token = self._token(permissions=["formula.read"])

        response = self.client.get(
            "/v1/recipes/download/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(404, response.status_code)

    def test_formula_read_user_can_request_manual_bom_sync(self) -> None:
        import json

        token = self._token(permissions=["formula.read"])

        response = self.client.post(
            "/v1/recipes/sync-bom",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual("pending", data["status"])
        self.assertEqual("bom", data["module"])
        self.assertTrue(data["include_disabled"])
        request_files = list((self.tmp_path / "tplus-sync-requests").glob("*.json"))
        self.assertEqual(1, len(request_files))
        payload = json.loads(request_files[0].read_text(encoding="utf-8"))
        self.assertEqual("bom", payload["module"])
        self.assertEqual("manual_bom_full_include_disabled", payload["mode"])
        self.assertTrue(payload["include_disabled"])

    def test_manual_bom_sync_requires_formula_read_permission(self) -> None:
        token = self._token(permissions=["production.schedule.read"])

        response = self.client.post(
            "/v1/recipes/sync-bom",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(403, response.status_code)

    def test_formula_read_user_can_calculate_recipe_cost_with_manual_prices(self) -> None:
        token = self._token(permissions=["formula.read"])

        response = self.client.post(
            "/v1/recipes/cost",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "query": "3027",
                "default_bom": "1",
                "manual_prices": {"C002": 25},
            },
        )

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(1, data["recipe_count"])
        recipe = data["recipes"][0]
        self.assertEqual("V1", recipe["version"])
        # 分价 = 比例 × 单价：C001 比例 0.8、C002 比例 0.2
        self.assertAlmostEqual(12.0, recipe["system_total"])   # 0.8*10 + 0.2*20
        self.assertAlmostEqual(13.0, recipe["current_total"])  # 0.8*10 + 0.2*25
        by_code = {line["child_code"]: line for line in recipe["lines"]}
        self.assertAlmostEqual(20.0, by_code["C002"]["system_price"])
        self.assertAlmostEqual(25.0, by_code["C002"]["current_price"])
        self.assertAlmostEqual(5.0, by_code["C002"]["current_amount"])  # 0.2 * 25

    def test_recipe_cost_requires_formula_read_permission(self) -> None:
        token = self._token(permissions=["production.schedule.read"])

        response = self.client.post(
            "/v1/recipes/cost",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "3027"},
        )

        self.assertEqual(403, response.status_code)


if __name__ == "__main__":
    unittest.main()
