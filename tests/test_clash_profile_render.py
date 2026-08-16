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
        self.assertEqual(providers["airport7"]["path"], "./providers/airport7.yaml")
        groups = _section(out, "proxy-groups")
        auto = next(g for g in groups if g["name"] == "自动选择")
        self.assertEqual(auto["use"], ["airport7"])

    def test_provider_is_file_backed_and_never_hits_airport(self) -> None:
        """回归保护：产物里的 provider 必须是 type: file，且不得出现订阅 URL。

        2026-08-15 起拉取在服务端（fetch.py）。客户端一旦回到 type: http，就会在
        家宽上直接撞机场的源 IP 封禁——实测 ICMP 通、TCP 全端口丢弃，持续 3 天以上，
        走任何节点也不行（HK/JP/US/SG 全 000，同节点访问 api.ipify.org 却正常）。

        顺带守住"订阅 URL 不进产物"：产物会被下载到客户端、可能被随手转发，
        而 URL 里的 token 能换出全部节点凭据（33 个节点共用一个账号级 uuid）。
        """
        out = self.render.render_profile([self.node], [self.provider])
        providers = _section(out, "proxy-providers")
        self.assertEqual(providers["airport7"]["type"], "file")
        self.assertNotIn("url", providers["airport7"])
        self.assertNotIn("proxy", providers["airport7"])
        self.assertNotIn("token=x", out)

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

    def test_claude_is_covered_in_rules_and_dns(self) -> None:
        """规则和 DNS 必须同时覆盖 Claude，缺一不可。

        2026-08-15 发现首版三处（规则、nameserver-policy、fallback-filter）**全漏**了
        Claude：网页版、桌面版、手机版和终端 CLI（api.anthropic.com）统统落到兜底
        MATCH,节点选择，走的正是要避开的机场共享 IP。

        只补规则不补 DNS 也不行：连接从自建节点出去、解析却从别的出口查，
        GeoDNS 会给出与出口不匹配的边缘 IP。
        """
        out = self.render.render_profile([self.node], [])
        lines = out.splitlines()
        for suffix in ("anthropic.com", "claude.ai", "claudeusercontent.com"):
            self.assertIn(f"  - DOMAIN-SUFFIX,{suffix},AI服务", lines)
            self.assertIn(f'"+.{suffix}":', out)
            self.assertIn(f'      - "+.{suffix}"', lines)

    def test_ai_domains_resolve_through_ai_group(self) -> None:
        # 规则把连接送去 AI服务，DNS 也必须同组，否则两边出口不一致。
        out = self.render.render_profile([self.node], [])
        self.assertIn("https://1.1.1.1/dns-query#AI服务", out)
        for volatile in ("+.openai.com", "+.chatgpt.com", "+.anthropic.com", "+.claude.ai"):
            block = out.split(f'"{volatile}":')[1].split('"+.')[0]
            self.assertIn("#AI服务", block)
            self.assertNotIn("#节点选择", block)

    def test_cloudflare_suffix_is_not_swallowed_by_ai_group(self) -> None:
        # 只有验证码子域该走自建节点；整个 cloudflare.com 压进去会把无关流量都塞给它。
        out = self.render.render_profile([self.node], [])
        lines = out.splitlines()
        self.assertIn("  - DOMAIN,challenges.cloudflare.com,AI服务", lines)
        self.assertNotIn("  - DOMAIN-SUFFIX,cloudflare.com,AI服务", lines)

    def test_ai_group_prefers_self_node_and_can_fall_back_to_a_named_node(self) -> None:
        """AI服务 默认自建节点，应急项必须是**具体节点**而不是「节点选择」组。

        2026-08-15 修：原先应急项写的是 GROUP_SELECT，而「节点选择」当时也指向自建节点，
        自建节点真挂了切过去出口不变，等于没切。
        """
        out = self.render.render_profile([self.node], [self.provider])
        groups = _section(out, "proxy-groups")
        ai = next(g for g in groups if g["name"] == "AI服务")
        self.assertEqual(ai["proxies"][0], "self-a")
        self.assertEqual(ai["use"], ["airport7"])
        self.assertNotIn("节点选择", ai["proxies"])

    def test_ai_group_without_provider_has_no_use(self) -> None:
        # 空 use 会让 mihomo 启动失败，一个订阅源都没有时必须省略。
        out = self.render.render_profile([self.node], [])
        groups = _section(out, "proxy-groups")
        ai = next(g for g in groups if g["name"] == "AI服务")
        self.assertNotIn("use", ai)

    def test_empty_self_nodes_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.render.render_profile([], [self.provider])


if __name__ == "__main__":
    unittest.main()
