from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class WeComDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        from app.integrations import wecom_docs

        cls.mod = wecom_docs

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]

    def test_strip_complex_values_removes_image_fields_only(self) -> None:
        values = {
            "菜品": [{"text": "烤全猪", "type": "text"}],
            "菜品参考图": [{"id": "img-1", "image_url": "https://wdcdn.qpic.cn/a"}],
            "价格": 12,
        }
        cleaned = self.mod.strip_complex_values(values)
        self.assertNotIn("菜品参考图", cleaned)
        self.assertEqual(values["菜品"], cleaned["菜品"])
        self.assertEqual(12, cleaned["价格"])

    def test_credentials_for_profile_requires_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(self.mod.WeComDocError):
                self.mod.credentials_for_profile("COMPANY_A")
        with patch.dict(
            os.environ,
            {"WECOM_COMPANY_A_CORP_ID": "corp", "WECOM_COMPANY_A_APP_SECRET": "sec"},
            clear=True,
        ):
            self.assertEqual(("corp", "sec"), self.mod.credentials_for_profile("COMPANY_A"))

    def test_doc_admin_users_parses_list_and_requires_value(self) -> None:
        with patch.dict(os.environ, {"WECOM_DOC_ADMIN_USERS": "WangHao; LiSi,"}, clear=True):
            self.assertEqual(["WangHao", "LiSi"], self.mod.doc_admin_users())
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(self.mod.WeComDocError):
                self.mod.doc_admin_users()


if __name__ == "__main__":
    unittest.main()
