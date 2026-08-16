"""Clash 配置合成：静态模板 + 运行时节点定义 → 单一 YAML 文本。

不解析 YAML：静态段原样拼接，动态段用 json.dumps 生成 flow-style 值
（YAML 1.2 是 JSON 超集，mihomo 照常解析），因此本模块零第三方依赖，
且节点名里的中文、引号、emoji 由标准库正确转义。

⚠️ 2026-08-15 起，机场节点由**服务端**拉取（见 fetch.py），产物里的 proxy-provider
是 `type: file`，客户端只读本地节点文件、不向机场发任何请求。

原先的写法是 `type: http` + 客户端自行按 interval 拉取，"服务端完全不接触机场"。
该写法自 2026-08-15 起确认在本环境不可用：机场对家宽出口做了源 IP 封禁
（ICMP 通但 TCP 全端口丢弃，持续 3 天以上），客户端无论怎么配都拉不到；
同刻境内服务器拉同一 URL 返回 200。详见 docs/superpowers/specs 的 Clash 合成器设计文档。
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
HEALTH_CHECK_INTERVAL = 300

# 客户端节点文件的存放位置，相对 mihomo 的工作目录（Clash Verge 是它的 profiles 根）。
# 本机的每日同步任务往这个路径写，mihomo 监听到文件变化后约 2 秒自动重载
# （2026-08-15 实测：覆盖文件后 core.log 出现 "[Provider] test's content update"，
# 节点数从 1 变 3，内核未重启。备用手段 PUT /providers/proxies/<name> 返回 204）。
PROVIDER_FILE_DIR = "./providers"

# ⚠️ 历史：这里曾是 `type: http` + `proxy: DIRECT` + `interval: 86400`，由客户端自己拉。
# 那个写法解决的是"mihomo 拉 provider 会走自己的规则链导致自举死锁"（2026-08-12 探针实测：
# 日志出现 "dial <组名> mihomo --> <订阅地址>"）。该问题真实存在，`proxy: DIRECT` 也确实
# 修好了它——但自 2026-08-15 起整条 http 路径已被移除，因为机场按源 IP 封了家宽，
# 直连与走任何节点都拉不到（HK/JP/US/SG 全部 000，同刻同节点访问 api.ipify.org 正常）。
# 拉取已上移到服务端 fetch.py。不要因为看到"客户端拉不到"就把 http provider 加回来。


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
                "type": "file",
                # 这个文件由本机每日同步任务从服务端取回覆盖；产物本身不含节点。
                #
                # ⚠️ 文件缺失**不会**让 mihomo 启动失败——2026-08-15 实测：内核照常起来，
                # 该 provider 静默变成 0 节点，组里只剩自建节点。失败形态是"机场节点凭空
                # 消失"而不是"起不来"，排查时容易往错的方向找。导入配置时务必确认
                # providers/ 下有对应文件。
                "path": f"{PROVIDER_FILE_DIR}/{provider_key(p['id'])}.yaml",
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
    #
    # ⚠️ 应急项必须是**具体节点**，不能是「节点选择」组（2026-08-15 修）。原先写的是
    # [*names, GROUP_SELECT]，而「节点选择」当时也指向自建节点——自建节点真挂了的时候
    # 切过去出口不变，等于没切，应急路径是坏的。改成挂 use，机场节点逐个可指定。
    ai_group: dict[str, Any] = {"name": GROUP_AI, "type": "select", "proxies": [*names]}
    if keys:
        ai_group["use"] = keys
    groups.append(ai_group)

    parts.append("proxy-groups: " + _dump(groups))
    parts.append("")

    parts.append("rules:")
    parts.append("  # 自建节点服务器地址直连，避免回环（由节点定义推导，勿手写）")
    parts.extend(_guard_rule(node["server"]) for node in self_nodes)
    parts.append(TEMPLATE_RULES.read_text(encoding="utf-8").lstrip("\n"))

    return "\n".join(parts)
