from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def _section(rendered: str, key: str):
    """取出 `key: {json}` 这一行的 JSON 值。渲染产物里动态段都是单行 flow-style。"""
    for line in rendered.splitlines():
        if line.startswith(f"{key}: "):
            return json.loads(line[len(key) + 2 :])
    return None


class ClashProfileRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]
        sys.path.insert(0, backend_root)
        from app.clash_profile import render

        self.render = render
        self.node = {"name": "self-a", "type": "vless", "server": "203.0.113.10", "port": 443}
        self.provider = {
            "id": 7,
            "name": "机场甲",
            "url": "https://example.com/sub?token=x",
            "enabled": True,
            "sort_order": 0,
        }

    def tearDown(self) -> None:
        sys.path[:] = self._old_sys_path
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]

    def test_no_provider_omits_auto_group(self) -> None:
        out = self.render.render_profile([self.node], [])
        groups = _section(out, "proxy-groups")
        names = [g["name"] for g in groups]
        self.assertNotIn("自动选择", names)
        self.assertIsNone(_section(out, "proxy-providers"))
        select = next(g for g in groups if g["name"] == "节点选择")
        self.assertNotIn("use", select)

    def test_enabled_providers_only(self) -> None:
        disabled = {**self.provider, "id": 8, "name": "机场乙", "enabled": False}
        out = self.render.render_profile([self.node], [self.provider, disabled])
        providers = _section(out, "proxy-providers")
        self.assertEqual(list(providers), ["airport7"])
        self.assertEqual(providers["airport7"]["url"], "https://example.com/sub?token=x")
        self.assertEqual(providers["airport7"]["path"], "./providers/airport7.yaml")
        groups = _section(out, "proxy-groups")
        auto = next(g for g in groups if g["name"] == "自动选择")
        self.assertEqual(auto["use"], ["airport7"])

    def test_select_group_name_is_locked(self) -> None:
        # DNS 段有约 24 处 "#节点选择" 引用组名，改名会让境外 DNS 全部失效。
        out = self.render.render_profile([self.node], [self.provider])
        groups = _section(out, "proxy-groups")
        self.assertIn("节点选择", [g["name"] for g in groups])
        self.assertIn("#节点选择", out)

    def test_ipv4_server_gets_cidr32_guard(self) -> None:
        out = self.render.render_profile([self.node], [])
        self.assertIn("  - IP-CIDR,203.0.113.10/32,DIRECT,no-resolve", out.splitlines())

    def test_ipv6_server_gets_cidr128_guard(self) -> None:
        node = {**self.node, "server": "2001:db8::1"}
        out = self.render.render_profile([node], [])
        self.assertIn("  - IP-CIDR6,2001:db8::1/128,DIRECT,no-resolve", out.splitlines())

    def test_domain_server_gets_domain_guard(self) -> None:
        node = {**self.node, "server": "node.example.com"}
        out = self.render.render_profile([node], [])
        self.assertIn("  - DOMAIN,node.example.com,DIRECT", out.splitlines())

    def test_node_name_with_cjk_and_quotes_round_trips(self) -> None:
        node = {**self.node, "name": '香港"节点" 01'}
        out = self.render.render_profile([node], [])
        self.assertEqual(_section(out, "proxies")[0]["name"], '香港"节点" 01')

    def test_ai_rules_point_to_ai_group(self) -> None:
        out = self.render.render_profile([self.node], [])
        self.assertIn("  - DOMAIN-SUFFIX,openai.com,AI服务", out.splitlines())
        self.assertNotIn("  - DOMAIN-SUFFIX,openai.com,节点选择", out.splitlines())

    def test_ai_group_prefers_self_node(self) -> None:
        out = self.render.render_profile([self.node], [self.provider])
        groups = _section(out, "proxy-groups")
        ai = next(g for g in groups if g["name"] == "AI服务")
        self.assertEqual(ai["proxies"][0], "self-a")
        self.assertNotIn("use", ai)

    def test_empty_self_nodes_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.render.render_profile([], [self.provider])


if __name__ == "__main__":
    unittest.main()
