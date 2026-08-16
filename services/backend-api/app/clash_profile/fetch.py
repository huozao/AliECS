"""机场订阅的服务端拉取与摘要。

为什么在服务端拉：机场按源 IP 封禁，家宽出口（含同一条宽带上的 webdock2）三天以上
TCP 全端口丢弃，客户端怎么配都拉不到；txecs 出口拉同一 URL 稳定 200。
backend-api 就跑在 txecs 上，所以拉取放这里，客户端只消费结果。

零第三方依赖：backend-api 的 requirements.txt 里没有 requests/httpx/PyYAML，
既有代码统一用 stdlib urllib（22 处）。摘要用正则而不是 YAML 解析，见 summarize()。
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from typing import NamedTuple


# 机场按 UA 区分客户端并返回对应格式。clash.meta 得到 Clash 格式的 YAML；
# 2026-08-12 实测：Mozilla/5.0 会被 403，不带 UA 会被 reset。
USER_AGENT = "clash.meta/v1.18.4"
FETCH_TIMEOUT_SECONDS = 30
# 正常订阅是几十 KB（实测 44911 字节）。留两个数量级余量，防止异常响应撑爆内存与数据库。
MAX_BODY_BYTES = 5 * 1024 * 1024

_PROXIES_SECTION = re.compile(r"^proxies:\s*$(.*?)(?=^\S)", re.MULTILINE | re.DOTALL)
_SERVER = re.compile(r"\bserver:\s*([^\s,}]+)")
_PORT = re.compile(r"\bport:\s*(\d+)")
_TYPE = re.compile(r"\btype:\s*([A-Za-z0-9_-]+)")


class Snapshot(NamedTuple):
    content: str
    node_count: int
    fingerprint: str
    userinfo: str


def fetch_subscription(url: str) -> tuple[str, str]:
    """拉一次订阅，返回 (正文, subscription-userinfo 头)。失败抛 RuntimeError。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_BODY_BYTES + 1)
            userinfo = response.headers.get("subscription-userinfo", "") or ""
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"机场返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # 家宽被封时就是这条：连接超时，无响应码。
        raise RuntimeError(f"连接机场失败：{exc}") from exc

    if len(raw) > MAX_BODY_BYTES:
        raise RuntimeError(f"订阅正文超过 {MAX_BODY_BYTES} 字节，拒绝存储")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"订阅正文不是 UTF-8：{exc}") from exc
    if "proxies:" not in content:
        # 有的机场按 UA 返回 base64 的 v2ray 链接列表；那种格式喂给 mihomo 的 file
        # provider 会静默变成 0 节点，宁可在这里失败得明显一点。
        raise RuntimeError("订阅正文不含 proxies:，不是 Clash 格式（检查订阅 URL 或机场设置）")
    return content, userinfo


def summarize(content: str) -> tuple[int, str]:
    """算出 (节点条目数, 指纹)。

    指纹只取 type/server/port 三元组，**刻意不含节点名**：机场会把「剩余流量：59.34 GB」
    「距离下次重置剩余：25 天」这类信息伪装成节点混在 proxies 里，它们的名字每天都在变
    （2026-08-12 实测），把名字算进指纹会导致天天误报"节点已变更"。

    而 type/server/port 恰好覆盖了真正会让节点失效的那种变更——2026-08-15 实测机场
    整批换代：协议、节点域名、端口段三者同时更换，旧域名 DNS 直接撤除，
    缓存里的节点全部作废。这三项任一变化都必须让用户看见。

    用正则而不是 YAML 解析：backend-api 无 PyYAML，且这里只需要三个标量字段。
    """
    match = _PROXIES_SECTION.search(content + "\n\x00")
    section = match.group(1) if match else ""
    types = _TYPE.findall(section)
    servers = _SERVER.findall(section)
    ports = _PORT.findall(section)
    if not (len(types) == len(servers) == len(ports)):
        # 三者数量不齐说明这份 YAML 的结构超出正则假设（例如节点带嵌套的
        # ws-opts.headers.Host）。此时退化成整段哈希：会有误报但不会漏报。
        digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
        return len(types), f"degraded:{digest[:32]}"

    triples = sorted({f"{t}|{s}|{p}" for t, s, p in zip(types, servers, ports)})
    digest = hashlib.sha256("\n".join(triples).encode("utf-8")).hexdigest()
    return len(types), digest[:32]


def fetch_snapshot(url: str) -> Snapshot:
    content, userinfo = fetch_subscription(url)
    node_count, fingerprint = summarize(content)
    if node_count == 0:
        raise RuntimeError("订阅解析出 0 个节点，拒绝覆盖已有快照")
    return Snapshot(content=content, node_count=node_count, fingerprint=fingerprint, userinfo=userinfo)
