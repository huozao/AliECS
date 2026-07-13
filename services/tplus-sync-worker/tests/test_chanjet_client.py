from __future__ import annotations

import unittest

from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.core.exceptions import ChanjetAPIError


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json
        return json.loads(self.text)


class FakeSession:
    def __init__(self, response):
        self.response = response

    def post(self, *args, **kwargs):
        return self.response


class FakeSettings:
    base_url = "https://openapi.example"
    timeout = 5
    app_key = "k"
    app_secret = "s"
    open_token = "t"


class ChanjetClientErrorTests(unittest.TestCase):
    def _client(self, response):
        return ChanjetClient(settings=FakeSettings(), session=FakeSession(response))

    def test_http_error_surfaces_business_message(self):
        body = '{"message": "存货编号：30122027-3027不唯一，请尝试修改该编号中的流水号后再操作"}'
        client = self._client(FakeResponse(500, body))
        with self.assertRaises(ChanjetAPIError) as ctx:
            client.post("/tplus/api/v2/inventory/Create", {"dto": {}})
        self.assertIn("不唯一", ctx.exception.business_message)
        self.assertIn("不唯一", str(ctx.exception))

    def test_http_error_nested_message_found(self):
        body = '{"result": {"Exception": {"Message": "父级错误包裹"}}}'
        client = self._client(FakeResponse(400, body))
        with self.assertRaises(ChanjetAPIError) as ctx:
            client.post("/x", {})
        self.assertEqual("父级错误包裹", ctx.exception.business_message)

    def test_http_error_without_json_body_keeps_generic_message(self):
        client = self._client(FakeResponse(502, "<html>bad gateway</html>"))
        with self.assertRaises(ChanjetAPIError) as ctx:
            client.post("/x", {})
        self.assertEqual("", ctx.exception.business_message)
        self.assertIn("HTTP 502", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
