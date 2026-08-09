"""BOM 必须用独立的小 page size，不能跟着全局 DEFAULT_PAGE_SIZE=500 走。

2026-08-09 生产实测 /tplus/api/v2/bom/QueryPage 的服务端耗时（219 行 enabled）：

    PageSize=5    2.1s
    PageSize=20   3.4s
    PageSize=100  14.6s
    PageSize=500  38.5s   ← 超过 REQUEST_TIMEOUT_READ=30，每轮必被掐断

BOM 每行要展开整棵子件树，耗时随 PageSize 超线性增长；同样 PageSize=500 的
inventory（609 行）只要 9 秒，所以不能靠调全局默认值解决。
后果：08-05 起定时全量的 bom 模块连续失败，存货/BOM 数据停更。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings, load_settings
from tplus_datahub.chanjet.pagination import paginate_query
from tplus_datahub.modules.bom.sync_bom import sync_bom


class _RecordingClient:
    def __init__(self):
        self.payloads = []

    def post(self, endpoint, payload):
        self.payloads.append(payload["param"].copy())
        return {"Result": {"Rows": []}}


def _settings(tmp: str, **overrides) -> Settings:
    base = dict(
        base_url="https://openapi.example.com",
        app_key="app-key",
        app_secret="app-secret",
        open_token="open-token",
        default_page_size=500,
        timeout_connect=5,
        timeout_read=30,
        output_dir=str(Path(tmp) / "output"),
        data_dir=str(Path(tmp) / "data"),
    )
    base.update(overrides)
    return Settings(**base)


class BomPageSizeTests(unittest.TestCase):
    def test_full_bom_sync_uses_bom_page_size_not_the_global_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp, default_page_size=500, bom_page_size=50)
            client = _RecordingClient()

            sync_bom(settings=settings, client=client, timestamp="20260809_120000")

            self.assertTrue(client.payloads)
            for param in client.payloads:
                self.assertEqual(50, param["PageSize"])

    def test_targeted_bom_query_also_uses_bom_page_size(self):
        """按父件编码查也走同一个接口，同样不能用 500。"""
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp, default_page_size=500, bom_page_size=50)
            client = _RecordingClient()

            sync_bom(settings=settings, client=client, timestamp="20260809_120000",
                     query_params={"Code": "HYD-4197PC"}, include_disabled=True)

            self.assertTrue(client.payloads)
            for param in client.payloads:
                self.assertEqual(50, param["PageSize"])

    def test_paginate_query_without_override_keeps_the_global_default(self):
        """其他模块（inventory 等）行为必须一个字节都不变。"""
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp, default_page_size=500)
            client = _RecordingClient()

            paginate_query(client=client, endpoint="/x", module_name="probe",
                           settings=settings, timestamp="20260809_120000")

            self.assertEqual(500, client.payloads[0]["PageSize"])

    def test_paginate_query_accepts_an_explicit_page_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp, default_page_size=500)
            client = _RecordingClient()

            paginate_query(client=client, endpoint="/x", module_name="probe",
                           settings=settings, timestamp="20260809_120000", page_size=20)

            self.assertEqual(20, client.payloads[0]["PageSize"])


class BomPageSizeSettingsTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("TPLUS_BOM_PAGE_SIZE")
        for name in ("CHANJET_APP_KEY", "CHANJET_APP_SECRET", "CHANJET_OPEN_TOKEN"):
            os.environ.setdefault(name, "x")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TPLUS_BOM_PAGE_SIZE", None)
        else:
            os.environ["TPLUS_BOM_PAGE_SIZE"] = self._old

    def test_defaults_to_50(self):
        os.environ.pop("TPLUS_BOM_PAGE_SIZE", None)
        self.assertEqual(50, load_settings(env_file="does-not-exist.env").bom_page_size)

    def test_reads_env_override(self):
        os.environ["TPLUS_BOM_PAGE_SIZE"] = "20"
        self.assertEqual(20, load_settings(env_file="does-not-exist.env").bom_page_size)


if __name__ == "__main__":
    unittest.main()
