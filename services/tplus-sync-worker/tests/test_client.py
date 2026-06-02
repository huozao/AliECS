import unittest
from dataclasses import replace

from config.settings import Settings
from tplus_datahub.chanjet.client import ChanjetClient


class FakeResponse:
    status_code = 200
    text = '{"ok": true}'

    def json(self):
        return {"ok": True}

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.last_url = None
        self.last_json = None
        self.last_headers = None
        self.last_timeout = None

    def post(self, url, json, headers, timeout):
        self.last_url = url
        self.last_json = json
        self.last_headers = headers
        self.last_timeout = timeout
        return FakeResponse()


class ChanjetClientTests(unittest.TestCase):
    def test_post_adds_credentials_to_headers_and_uses_json_body(self):
        settings = Settings(
            base_url="https://openapi.example.com",
            app_key="app-key",
            app_secret="app-secret",
            open_token="open-token",
            default_page_size=500,
            timeout_connect=5,
            timeout_read=30,
            output_dir="output",
            data_dir="data",
        )
        session = FakeSession()
        client = ChanjetClient(settings=settings, session=session)

        result = client.post("/tplus/api/v2/bom/QueryPage", {"PageIndex": 1})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(session.last_url, "https://openapi.example.com/tplus/api/v2/bom/QueryPage")
        self.assertEqual(session.last_headers["Content-Type"], "application/json")
        self.assertEqual(session.last_headers["appKey"], "app-key")
        self.assertEqual(session.last_headers["appSecret"], "app-secret")
        self.assertEqual(session.last_headers["openToken"], "open-token")
        self.assertEqual(session.last_timeout, (5, 30))
        self.assertEqual(session.last_json["PageIndex"], 1)
        self.assertNotIn("appKey", session.last_json)
        self.assertNotIn("appSecret", session.last_json)
        self.assertNotIn("openToken", session.last_json)


if __name__ == "__main__":
    unittest.main()
