from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
HEALTH_PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "health" / "index.html"


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class WechatLoginQrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        from app.routers import ops as main_module

        cls.main = main_module

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]

    def test_login_qr_endpoint_returns_gateway_image_payload(self) -> None:
        old_urlopen = self.main.urllib.request.urlopen
        old_url = self.main.os.environ.get("OPENCLAW_WECHAT_LOGIN_QR_URL")
        self.main.os.environ["OPENCLAW_WECHAT_LOGIN_QR_URL"] = "http://openclaw.local/wechat/login-qr"

        def fake_urlopen(request, timeout):
            self.assertEqual(5, timeout)
            self.assertIn("/wechat/login-qr", request.full_url)
            return FakeResponse({"qr_image_base64": "data:image/png;base64,AAAA", "expires_at": "2026-06-14T12:00:00Z"})

        self.main.urllib.request.urlopen = fake_urlopen
        try:
            payload = self.main.ops_wechat_login_qr(_={"sub": "admin"})
        finally:
            self.main.urllib.request.urlopen = old_urlopen
            if old_url is None:
                self.main.os.environ.pop("OPENCLAW_WECHAT_LOGIN_QR_URL", None)
            else:
                self.main.os.environ["OPENCLAW_WECHAT_LOGIN_QR_URL"] = old_url

        self.assertEqual("data:image/png;base64,AAAA", payload["qr_image_base64"])
        self.assertEqual("gateway", payload["source"])

    def test_health_page_has_add_wechat_entry(self) -> None:
        html = HEALTH_PAGE.read_text(encoding="utf-8")

        self.assertIn("渠道与其他", html)
        self.assertIn("添加新微信", html)
        self.assertIn("wechatQrModal", html)
        self.assertIn("/v1/ops/wechat/login-qr", html)


if __name__ == "__main__":
    unittest.main()
