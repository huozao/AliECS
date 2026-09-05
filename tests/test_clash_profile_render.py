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
        self.provider_content = """proxies:
  - {name: 剩余流量：1 GB, type: vless, server: pseudo.example.com, port: 443}
  - {name: 机场香港, type: vless, server: hk.example.com, port: 443, uuid: u}
  - name: 机场日本
    type: vless
    server: jp.example.com
    port: 443
    uuid: u2
"""

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

    def test_webdock_target_keeps_routes_but_exposes_docker_bridge(self) -> None:
        out = self.render.render_profile([self.node], [], target="webdock")
        self.assertIn("allow-lan: true", out)
        self.assertIn("bind-address: 172.17.0.1", out)
        self.assertNotIn("\ntun:\n", out)
        self.assertIn("DOMAIN-SUFFIX,openai.com,AI服务", out)

    def test_unknown_target_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.render.render_profile([self.node], [], target="unknown")

    def test_mobile_target_embeds_snapshot_nodes_without_provider_files(self) -> None:
        out = self.render.render_profile(
            [self.node],
            [self.provider],
            target="mobile",
            provider_contents={self.provider["id"]: self.provider_content},
        )
        self.assertNotIn("proxy-providers:", out)
        self.assertIn("机场香港", out)
        self.assertIn("机场日本", out)
        self.assertNotIn("剩余流量：1 GB", out)
        self.assertNotIn("token=x", out)
        groups = _section(out, "proxy-groups")
        dukascopy = next(g for g in groups if g["name"] == "Dukascopy")
        self.assertEqual(dukascopy["proxies"], ["机场香港", "机场日本"])

    def test_mobile_target_requires_every_enabled_snapshot(self) -> None:
        with self.assertRaisesRegex(ValueError, "没有可用快照"):
            self.render.render_profile([self.node], [self.provider], target="mobile", provider_contents={})

    def test_mobile_target_accepts_four_space_provider_indentation(self) -> None:
        # Real airport exports use four spaces below ``proxies:``; the parser
        # must not assume the two-space indentation used by the fixture above.
        content = self.provider_content.replace("\n  - ", "\n    - ")
        out = self.render.render_profile(
            [self.node],
            [self.provider],
            target="mobile",
            provider_contents={self.provider["id"]: content},
        )
        self.assertIn("机场香港", out)
        self.assertIn("机场日本", out)

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

    def test_dukascopy_group_never_contains_self_nodes(self) -> None:
        """Dukascopy 组不含自建节点，这一点必须是结构性的而不是靠人工核验。

        2026-08-17 曾在客户端 profile 扩展里靠 exclude-filter 做隔离，08-21 的一次
        编辑把 filter 改丢了，直到 08-25 排查代理时才发现——期间批量抓数据和 AI
        账号共用出口 IP。放进产物并在这里断言，是为了让它改不掉。

        两层理由：Dukascopy 按 IP 限流且是「突发配额 + 长封锁」（实测 ≥15 小时），
        以及自建节点按流量计费，批量补历史会直接吃额度。
        """
        out = self.render.render_profile([self.node], [self.provider])
        groups = _section(out, "proxy-groups")
        duka = next(g for g in groups if g["name"] == "Dukascopy")
        self.assertEqual(duka["use"], ["airport7"])
        self.assertNotIn("proxies", duka)
        self.assertNotIn("self-a", json.dumps(duka, ensure_ascii=False))

    def test_dukascopy_group_degrades_to_direct_without_provider(self) -> None:
        """没有订阅源时降级 DIRECT，**不能省略整个组**。

        规则表是静态模板，dukascopy 那一行恒存在；组不存在会让 mihomo 整份配置
        加载失败，失败形态是内核起不来而不是某条规则失效。
        """
        out = self.render.render_profile([self.node], [])
        groups = _section(out, "proxy-groups")
        duka = next(g for g in groups if g["name"] == "Dukascopy")
        self.assertEqual(duka["proxies"], ["DIRECT"])
        self.assertNotIn("use", duka)

    def test_github_group_defaults_to_self_node(self) -> None:
        # git 长连接被换节点会断，默认要稳；面板上仍可改选机场节点。
        out = self.render.render_profile([self.node], [self.provider])
        groups = _section(out, "proxy-groups")
        github = next(g for g in groups if g["name"] == "GitHub")
        self.assertEqual(github["proxies"][0], "self-a")
        self.assertEqual(github["use"], ["airport7"])

    def test_direct_group_exists_and_prefers_direct(self) -> None:
        # Windows Update 一类规则指向这个组而不是内置 DIRECT，为的是留一个面板开关。
        out = self.render.render_profile([self.node], [])
        groups = _section(out, "proxy-groups")
        direct = next(g for g in groups if g["name"] == "全球直连")
        self.assertEqual(direct["proxies"][0], "DIRECT")

    def test_every_rule_target_group_exists(self) -> None:
        """规则里引用的每个策略组都必须在产物里存在。

        引用一个不存在的组不会只让那条规则失效，而是让 mihomo **整份配置加载失败**。
        无订阅源是最容易漏的分支，所以两种情况都测。
        """
        for providers in ([], [self.provider]):
            out = self.render.render_profile([self.node], providers)
            names = {g["name"] for g in _section(out, "proxy-groups")}
            builtin = {"DIRECT", "REJECT", "no-resolve"}
            for line in out.splitlines():
                stripped = line.strip()
                if not stripped.startswith("- ") or "," not in stripped:
                    continue
                target = stripped.split(",")[-1].strip()
                if target in builtin or target.endswith(")"):
                    continue
                self.assertIn(target, names | builtin, f"规则指向了不存在的组：{stripped}")

    def test_new_ai_domains_are_covered_in_rules_and_dns(self) -> None:
        """2026-09-02 新增的 AI 域名，同样三处齐全。

        claude.com 是**另一个根域**，anthropic.com 与 claude.ai 都盖不到——当天日志
        实测 code.claude.com 和 platform.claude.com 都落在兜底 MATCH。
        """
        out = self.render.render_profile([self.node], [])
        lines = out.splitlines()
        for suffix in (
            "claude.com",
            "oaistatsig.com",
            "grok.com",
            "x.ai",
            "githubcopilot.com",
            "cursor.com",
            "cursor.sh",
            "perplexity.ai",
            "gemini.google.com",
            "aistudio.google.com",
            "generativelanguage.googleapis.com",
        ):
            self.assertIn(f"  - DOMAIN-SUFFIX,{suffix},AI服务", lines)
            self.assertIn(f'"+.{suffix}":', out)
            self.assertIn(f'      - "+.{suffix}"', lines)

    def test_gemini_rules_precede_the_generic_google_rules(self) -> None:
        """generativelanguage.googleapis.com 必须排在 googleapis.com 之前。

        规则是首次命中即止，顺序反了 Gemini 就被 googleapis.com 吃掉走「节点选择」。
        顺序只靠位置维持，这条断言是唯一的保护。
        """
        lines = self.render.render_profile([self.node], []).splitlines()
        specific = lines.index("  - DOMAIN-SUFFIX,generativelanguage.googleapis.com,AI服务")
        generic = lines.index("  - DOMAIN-SUFFIX,googleapis.com,节点选择")
        self.assertLess(specific, generic)

    def test_dukascopy_and_github_rules_point_to_their_own_groups(self) -> None:
        out = self.render.render_profile([self.node], [self.provider])
        lines = out.splitlines()
        self.assertIn("  - DOMAIN-SUFFIX,dukascopy.com,Dukascopy", lines)
        for suffix in ("github.com", "githubusercontent.com", "githubassets.com", "ghcr.io"):
            self.assertIn(f"  - DOMAIN-SUFFIX,{suffix},GitHub", lines)
            self.assertNotIn(f"  - DOMAIN-SUFFIX,{suffix},节点选择", lines)

    def test_dukascopy_and_github_dns_follow_their_own_groups(self) -> None:
        # 连接从一个出口走、DNS 从另一个出口查，GeoDNS 会给出不匹配的边缘 IP。
        out = self.render.render_profile([self.node], [self.provider])
        for domain, group in (("+.dukascopy.com", "#Dukascopy"), ("+.github.com", "#GitHub")):
            block = out.split(f'"{domain}":')[1].split('"+.')[0]
            self.assertIn(group, block)

    def test_system_update_traffic_goes_to_the_direct_group(self) -> None:
        """Windows Update 与 svchost 走直连组。

        2026-08-18 实测：订阅规则表里没有 windowsupdate 条目，全部吃兜底走代理，
        当天 svchost.exe 烧掉 734.9MB；裸 IP 的 Delivery Optimization 缓存节点连
        域名都没有，只能靠 PROCESS-NAME 兜住。
        """
        lines = self.render.render_profile([self.node], []).splitlines()
        for suffix in ("windowsupdate.com", "update.microsoft.com", "delivery.mp.microsoft.com"):
            self.assertIn(f"  - DOMAIN-SUFFIX,{suffix},全球直连", lines)
        self.assertIn("  - PROCESS-NAME,svchost.exe,全球直连", lines)

    def test_provider_excludes_pseudo_nodes(self) -> None:
        """机场的套餐提示伪节点必须在 provider 层就被排掉。

        2026-09-02 实测漏掉它的后果：伪节点排在节点列表最前，`select` 组默认选中
        第一个，`Dukascopy` 组开箱就指向「剩余流量：16.99 GB」，经它请求
        datafeed.dukascopy.com 返回 503；`自动选择`（url-test）落在「套餐到期」上。
        **两个组都是坏的，而配置本身不报任何错。**

        加在 provider 上是刻意的：一处生效，所有 use 它的组都干净。
        """
        out = self.render.render_profile([self.node], [self.provider])
        providers = _section(out, "proxy-providers")
        pattern = providers["airport7"]["exclude-filter"]
        for keyword in ("剩余流量", "距离下次重置", "套餐到期"):
            self.assertIn(keyword, pattern)

    def test_pseudo_node_filter_actually_matches_real_airport_names(self) -> None:
        """断言本身要先验一次：拿机场真实用过的名字跑一遍正则。

        只断言「字段里有这几个关键词」是不够的——写成一个语法上正确但匹配不上的
        正则，上面那条测试照样绿。这里直接用 2026-09-02 从节点文件里取到的两个真名
        （以及一个必须**不**被误伤的正常节点名）过一遍。
        """
        import re

        out = self.render.render_profile([self.node], [self.provider])
        pattern = _section(out, "proxy-providers")["airport7"]["exclude-filter"]
        for pseudo in ("剩余流量：16.99 GB", "距离下次重置剩余：7 天", "套餐到期：2026-10-09"):
            self.assertTrue(re.search(pattern, pseudo), f"应被排除却没匹配：{pseudo}")
        for real in ("【1x】香港 01", "【1x】美国 06", "self-a"):
            self.assertIsNone(re.search(pattern, real), f"正常节点被误伤：{real}")

    def test_empty_self_nodes_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.render.render_profile([], [self.provider])


if __name__ == "__main__":
    unittest.main()
