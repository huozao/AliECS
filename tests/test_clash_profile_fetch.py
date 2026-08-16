from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"

# 机场订阅的真实形状（占位地址）：真实节点与"信息伪节点"混在同一个 proxies 列表里，
# 后者的名字每天都在变——这正是指纹不能包含节点名的原因。
SUB_DAY1 = """proxies:
  - {name: 剩余流量：59.34 GB, type: vless, server: hk1.example.com, port: 58811, uuid: u}
  - {name: 距离下次重置剩余：25 天, type: vless, server: hk1.example.com, port: 58811, uuid: u}
  - {name: 【1x】香港 01, type: vless, server: hk1.example.com, port: 58811, uuid: u}
  - {name: 【1x】日本 01, type: vless, server: jp1.example.com, port: 58814, uuid: u}
proxy-groups:
  - {name: g, type: select, proxies: [DIRECT]}
"""

# 次日：只有信息伪节点的名字变了，真实节点一模一样。指纹必须保持不变。
SUB_DAY2 = SUB_DAY1.replace("59.34 GB", "58.10 GB").replace("25 天", "24 天")

# 机场换代：协议 vless→ss、域名换掉、端口换掉。指纹必须变。
SUB_ROTATED = """proxies:
  - {name: 【1x】香港 01, type: ss, server: hk1.example.net, port: 52361, password: p}
  - {name: 【1x】日本 01, type: ss, server: jp1.example.net, port: 52361, password: p}
"""


class ClashProfileFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]
        sys.path.insert(0, backend_root)
        from app.clash_profile import fetch

        self.fetch = fetch

    def tearDown(self) -> None:
        sys.path[:] = self._old_sys_path
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]

    def test_fingerprint_ignores_volatile_pseudo_nodes(self) -> None:
        """流量数字每天变，指纹不能跟着变，否则后台天天误报"节点已变更"。"""
        _, day1 = self.fetch.summarize(SUB_DAY1)
        _, day2 = self.fetch.summarize(SUB_DAY2)
        self.assertEqual(day1, day2)

    def test_fingerprint_detects_protocol_and_host_rotation(self) -> None:
        """2026-08-15 实测的真实故障：ss→vless、换域名、换端口，旧节点全部作废。"""
        _, before = self.fetch.summarize(SUB_DAY1)
        _, after = self.fetch.summarize(SUB_ROTATED)
        self.assertNotEqual(before, after)

    def test_node_count_matches_proxies_entries(self) -> None:
        count, _ = self.fetch.summarize(SUB_DAY1)
        self.assertEqual(count, 4)

    def test_proxy_groups_section_is_not_counted(self) -> None:
        # proxy-groups 里也有 type:，只统计 proxies 段才不会把组算成节点。
        count, _ = self.fetch.summarize(SUB_DAY1)
        self.assertNotEqual(count, 5)

    def test_non_clash_body_is_rejected_loudly(self) -> None:
        """有的机场按 UA 返回 base64 的 v2ray 链接；喂给 file provider 会静默变 0 节点。"""
        import urllib.request

        class _FakeResponse:
            headers = {"subscription-userinfo": ""}

            def read(self, _size: int) -> bytes:
                return b"dm1lc3M6Ly9leGFtcGxl"

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda *_a, **_k: _FakeResponse()
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self.fetch.fetch_subscription("https://example.com/sub")
        finally:
            urllib.request.urlopen = original
        self.assertIn("proxies:", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
