#!/usr/bin/env python3
"""结构性变更必须与导航文档同批；导航文档落后代码太久时告警。

两道判据性质不同，刻意分开：

- 硬门（--range）落在**连接处**：本次改动新增/删除了 router、service 或迁移，
  而 `docs/project-ai-map.md` 不在同一批 diff 里，就是漏登记。只查文档或只查
  代码都会得到「正常」。改 typo、改测试不产生 A/D 条目，所以不会误报。
- 软告警（--staleness）落在**时间差**：文件都在、条目也在，但内容早已过期。
  这类没有 A/D 条目，硬门看不见。它只 WARN 不 FAIL——常态红灯会被无视，
  等于没装。

时间戳一律取 Git 提交时间，不在文档里写「已对标 YYYY-MM-DD」这类标记：
那种标记由写文档的人自己填，没对标也能填上，和「注册成功≠会被执行」是同一
个自证陷阱。
"""

from __future__ import annotations

import argparse
import subprocess
import sys

NAV_DOC = "docs/project-ai-map.md"
DEFAULT_STALE_DAYS = 14
CODE_ROOTS = ["services", "db/migrations", "deploy/ecs"]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8").strip()


def structural_changes(base: str, head: str) -> list[str]:
    """只认新增/删除：改动内容不要求动导航，出现和消失才要求。"""
    raw = git("diff", "--name-status", "--diff-filter=AD", f"{base}...{head}")
    hits: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        path = path.strip()
        if path.startswith("services/backend-api/app/routers/") and path.endswith(".py"):
            hits.append(f"{status} {path}")
        elif path.startswith("db/migrations/") and path.endswith(".sql"):
            hits.append(f"{status} {path}")
        elif path.startswith("services/") and path.count("/") == 1:
            hits.append(f"{status} {path}")
    return hits


def touched(base: str, head: str) -> set[str]:
    return {p for p in git("diff", "--name-only", f"{base}...{head}").splitlines() if p}


def last_commit_ts(pathspec: str) -> int:
    out = git("log", "-1", "--format=%ct", "--", pathspec)
    return int(out) if out else 0


def check_range(base: str, head: str) -> list[str]:
    hits = structural_changes(base, head)
    if not hits:
        print("doc freshness: 本次没有结构性新增/删除，硬门不适用")
        return []
    if NAV_DOC in touched(base, head):
        print(f"doc freshness: {len(hits)} 处结构性变更，{NAV_DOC} 已同批更新")
        return []
    return [
        f"结构性变更未同批更新 {NAV_DOC}:\n    " + "\n    ".join(hits)
    ]


def check_staleness(days: int) -> list[str]:
    doc_ts = last_commit_ts(NAV_DOC)
    if not doc_ts:
        return [f"missing doc: {NAV_DOC}"]
    code_ts = max(last_commit_ts(root) for root in CODE_ROOTS)
    lag_days = (code_ts - doc_ts) / 86400
    if lag_days > days:
        print(
            f"::warning::{NAV_DOC} 比代码落后 {lag_days:.1f} 天（阈值 {days}）。"
            "文件还在、条目也在，但内容可能已经过期——硬门查不到这一类。"
        )
    else:
        print(f"doc freshness: {NAV_DOC} 落后代码 {lag_days:.1f} 天（阈值 {days}）")
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--range", nargs=2, metavar=("BASE", "HEAD"))
    parser.add_argument("--staleness", action="store_true")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    args = parser.parse_args()
    if not args.range and not args.staleness:
        parser.error("至少给一个：--range 或 --staleness")

    errors: list[str] = []
    if args.range:
        errors.extend(check_range(*args.range))
    if args.staleness:
        errors.extend(check_staleness(args.stale_days))

    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
