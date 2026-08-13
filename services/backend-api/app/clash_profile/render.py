"""Clash 配置合成：静态模板 + 运行时节点定义 → 单一 YAML 文本。

不解析 YAML：静态段原样拼接，动态段用 json.dumps 生成 flow-style 值
（YAML 1.2 是 JSON 超集，mihomo 照常解析），因此本模块零第三方依赖，
且节点名里的中文、引号、emoji 由标准库正确转义。

机场节点不在这里拉取——它们由客户端 mihomo 通过 proxy-providers 自行获取
并按 interval 定期刷新，服务端完全不接触机场。
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any


_HERE = Path(__file__).resolve().parent
TEMPLATE_BASE = _HERE / "template_base.yaml"
TEMPLATE_RULES = _HERE / "template_rules.yaml"

# ⚠️ template_base.yaml 的 DNS 段有约 24 处 "#节点选择" 引用这个组名（# 后是代理组名）。
# 改名会让境外 DNS 解析全部失效，症状间歇且难排查。tests 里有断言保护。
GROUP_SELECT = "节点选择"
GROUP_AUTO = "自动选择"
GROUP_AI = "AI服务"

HEALTH_CHECK_URL = "https://www.gstatic.com/generate_204"
PROVIDER_INTERVAL = 86400
HEALTH_CHECK_INTERVAL = 300

# ⚠️ mihomo 拉取 proxy-provider 时会走自己的规则链，而不是绕开它（2026-08-12 本地探针实测：
# 只有 MATCH 规则时，日志出现 "dial <组名> mihomo --> <订阅地址>"，拉取被丢进代理组）。
# 机场订阅域名通常解析到境外 IP，不命中 GEOIP,CN，于是一路掉到 MATCH,节点选择 ——
# 「拉订阅」这件事本身要先有可用代理，形成自举依赖，首次导入时必然失败（节点数 0）。
#
# 只能直连，不能走自建节点：实测机场对境外机房 IP 是拒绝的（aliecs 156ms 快速失败），
# 而国内直连正常（txecs 200）。这也正是 Clash Verge 现有 remote profile 的既有行为
# ——它默认不带 self_proxy，同样是直连拉取，周期同为 24 小时。
PROVIDER_FETCH_PROXY = "DIRECT"


def provider_key(provider_id: int) -> str:
    """provider 的 YAML key。用数据库 id 而非机场名，避免中文与特殊字符进 key。"""
    return f"airport{provider_id}"


def _guard_rule(server: str) -> str:
    """节点服务器地址必须直连，否则 TUN 模式下可能回环。地址来自节点定义，不硬编码。"""
    try:
        addr = ipaddress.ip_address(server)
    except ValueError:
        return f"  - DOMAIN,{server},DIRECT"
    if addr.version == 4:
        return f"  - IP-CIDR,{server}/32,DIRECT,no-resolve"
    return f"  - IP-CIDR6,{server}/128,DIRECT,no-resolve"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_profile(self_nodes: list[dict], providers: list[dict]) -> str:
    if not self_nodes:
        raise ValueError("自建节点定义为空，拒绝生成配置：AI服务 组会因此为空并导致 mihomo 启动失败")

    names = [node["name"] for node in self_nodes]
    active = sorted(
        (p for p in providers if p.get("enabled", True)),
        key=lambda p: (p.get("sort_order", 0), p["id"]),
    )
    keys = [provider_key(p["id"]) for p in active]

    parts: list[str] = [TEMPLATE_BASE.read_text(encoding="utf-8").rstrip("\n"), ""]

    parts.append("proxies: " + _dump(self_nodes))
    parts.append("")

    if active:
        parts.append("proxy-providers: " + _dump({
            provider_key(p["id"]): {
                "type": "http",
                "url": p["url"],
                "proxy": PROVIDER_FETCH_PROXY,
                "interval": PROVIDER_INTERVAL,
                # 已拉取过的副本落在这里；后续拉取失败时 mihomo 从它恢复节点，不会清空
                # （2026-08-12 实测：URL 指向死端口重启，缓存里的节点照常加载且不报 error）。
                "path": f"./providers/{provider_key(p['id'])}.yaml",
                "health-check": {
                    "enable": True,
                    "url": HEALTH_CHECK_URL,
                    "interval": HEALTH_CHECK_INTERVAL,
                },
            }
            for p in active
        }))
        parts.append("")

    select_group: dict[str, Any] = {"name": GROUP_SELECT, "type": "select"}
    if keys:
        select_group["proxies"] = [*names, GROUP_AUTO, "DIRECT"]
        select_group["use"] = keys
    else:
        # 空的 use 或空的 url-test 组会让 mihomo 启动失败，所以一个 provider 都没有时整段省略。
        select_group["proxies"] = [*names, "DIRECT"]

    groups: list[dict[str, Any]] = [select_group]
    if keys:
        groups.append({
            "name": GROUP_AUTO,
            "type": "url-test",
            "use": keys,
            "url": HEALTH_CHECK_URL,
            "interval": HEALTH_CHECK_INTERVAL,
            "tolerance": 50,
        })
    # AI 服务默认锁自建节点：机场共享 IP 容易触发 ChatGPT / Claude 的风控。
    groups.append({"name": GROUP_AI, "type": "select", "proxies": [*names, GROUP_SELECT]})

    parts.append("proxy-groups: " + _dump(groups))
    parts.append("")

    parts.append("rules:")
    parts.append("  # 自建节点服务器地址直连，避免回环（由节点定义推导，勿手写）")
    parts.extend(_guard_rule(node["server"]) for node in self_nodes)
    parts.append(TEMPLATE_RULES.read_text(encoding="utf-8").lstrip("\n"))

    return "\n".join(parts)
