"""给本机每日同步任务用的命令行入口。

用法（在 txecs 上，容器内执行）：

    docker exec business-cn-backend-api-1 python -m app.clash_profile.cli list
    docker exec business-cn-backend-api-1 python -m app.clash_profile.cli refresh
    docker exec business-cn-backend-api-1 python -m app.clash_profile.cli nodes airport1
    docker exec business-cn-backend-api-1 python -m app.clash_profile.cli profile mobile

为什么不走 HTTP：后台接口挂在 require_admin（SSO）后面，无人值守的定时任务过不去；
而把下载端点开成公网免鉴权就变成了对外分发订阅，是这次刻意避开的形态。
走 docker exec 的前提是已经能 SSH 到 txecs，不额外扩大任何访问面。

**stdout 契约**：`nodes` 只输出节点文件正文，不输出任何提示信息——调用方会把它直接
重定向成文件。诊断信息一律走 stderr。
"""

from __future__ import annotations

import sys
from contextlib import closing

from app.clash_profile import store
from app.clash_profile.fetch import fetch_snapshot
from app.clash_profile.render import load_self_nodes, provider_key, render_profile
from app.core import _conn


def _providers() -> list[dict]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, url, enabled, sort_order FROM clash_profile_providers ORDER BY sort_order, id"
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "key": provider_key(r[0]),
            "name": r[1],
            "url": r[2],
            "enabled": r[3],
            "sort_order": r[4],
        }
        for r in rows
    ]


def _resolve(token: str) -> dict | None:
    """接受数据库 id 或 provider key（airport1）两种写法。"""
    for provider in _providers():
        if token == str(provider["id"]) or token == provider["key"]:
            return provider
    return None


def cmd_list() -> int:
    for provider in _providers():
        state = "启用" if provider["enabled"] else "停用"
        print(f"{provider['id']}\t{provider['key']}\t{state}\t{provider['name']}")
    return 0


def cmd_refresh() -> int:
    """拉取全部启用的订阅源。任一失败不影响其他源，退出码非 0 供定时任务告警。"""
    failures = 0
    active = [p for p in _providers() if p["enabled"]]
    if not active:
        print("没有启用的订阅源", file=sys.stderr)
        return 1
    for provider in active:
        try:
            snapshot = fetch_snapshot(provider["url"])
        except RuntimeError as exc:
            store.save_error(provider["id"], str(exc))
            print(f"[失败] {provider['key']} {provider['name']}：{exc}", file=sys.stderr)
            failures += 1
            continue
        changed = store.save_snapshot(provider["id"], snapshot)
        flag = "节点已变更，客户端需重新导入配置" if changed else "无变化"
        print(
            f"[成功] {provider['key']} {provider['name']}："
            f"{snapshot.node_count} 个节点，指纹 {snapshot.fingerprint}，{flag}",
            file=sys.stderr,
        )
    return 1 if failures else 0


def cmd_nodes(token: str) -> int:
    provider = _resolve(token)
    if provider is None:
        print(f"找不到订阅源：{token}", file=sys.stderr)
        return 1
    content = store.read_content(provider["id"])
    if not content:
        print(f"{provider['key']} 还没有可用快照，先跑一次 refresh", file=sys.stderr)
        return 1
    sys.stdout.write(content)
    return 0


def cmd_profile(target: str = "desktop") -> int:
    """Write a complete generated profile to stdout for an authenticated operator."""
    try:
        providers = _providers()
        contents = None
        if target == "mobile":
            contents = {
                provider["id"]: store.read_content(provider["id"])
                for provider in providers
                if provider["enabled"]
            }
        profile = render_profile(
            load_self_nodes(), providers, target=target, provider_contents=contents
        )
    except ValueError as exc:
        print(f"生成 Clash 配置失败：{exc}", file=sys.stderr)
        return 1
    sys.stdout.write(profile)
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    command, args = argv[0], argv[1:]
    if command == "list":
        return cmd_list()
    if command == "refresh":
        return cmd_refresh()
    if command == "nodes":
        if len(args) != 1:
            print("用法：nodes <provider_id|provider_key>", file=sys.stderr)
            return 2
        return cmd_nodes(args[0])
    if command == "profile":
        if len(args) > 1:
            print("用法：profile [desktop|webdock|mobile]", file=sys.stderr)
            return 2
        return cmd_profile(args[0] if args else "desktop")
    print(f"未知命令：{command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
