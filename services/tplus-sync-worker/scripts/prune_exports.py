"""一次性清理 output/excel 下堆积的历史导出（按前缀各保留最新 N 个）。

平时每次同步产出新文件后会自动按前缀清理（见 storage/retention.py），本脚本用于
首次清理存量积压、或手动核对。默认 dry-run 只打印将删列表，加 --apply 才真正删除。

必须在 **worker 容器** 内运行（backend 对该卷是只读挂载，删不了）：
    docker exec -it ecs-tplus-sync-worker-1 python scripts/prune_exports.py            # 预览
    docker exec -it ecs-tplus-sync-worker-1 python scripts/prune_exports.py --apply     # 实删
可选：--keep N 覆盖每类保留数量，--dir PATH 覆盖目录。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))  # tplus_datahub.*
sys.path.insert(0, str(_ROOT))  # config.*

from config.settings import load_settings  # noqa: E402
from tplus_datahub.storage.retention import (  # noqa: E402
    resolve_retention_keep,
    prune_exports,
)

_STAMP_RE = re.compile(r"^(.+)_\d{8}_\d{6}\.xlsx$")


def _default_excel_dir() -> Path:
    return load_settings(validate=False).output_root / "excel"


def _discover_prefixes(directory: Path) -> list[str]:
    prefixes: set[str] = set()
    for path in directory.glob("*.xlsx"):
        if path.name.startswith("~$"):
            continue
        match = _STAMP_RE.match(path.name)
        if match:
            prefixes.add(match.group(1))
    return sorted(prefixes)


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 output/excel 历史导出，每类保留最新 N 个")
    parser.add_argument("--dir", type=Path, default=None, help="excel 目录（默认取 OUTPUT_DIR/excel）")
    parser.add_argument("--keep", type=int, default=None, help="每类保留数量（默认 TPLUS_EXPORT_RETENTION 或 48）")
    parser.add_argument("--apply", action="store_true", help="真正删除（缺省为 dry-run 只打印）")
    args = parser.parse_args()

    directory = args.dir or _default_excel_dir()
    keep = resolve_retention_keep(args.keep)
    if not directory.exists():
        print(f"目录不存在：{directory}")
        return 0

    prefixes = _discover_prefixes(directory)
    print(f"目录：{directory}　每类保留：{keep}　模式：{'实删' if args.apply else 'dry-run'}")
    print(f"识别到 {len(prefixes)} 类前缀：{', '.join(prefixes) or '（无）'}")

    total = 0
    for prefix in prefixes:
        affected = prune_exports(directory, prefix, keep=keep, dry_run=not args.apply)
        total += len(affected)
        if affected:
            verb = "已删除" if args.apply else "将删除"
            print(f"  [{prefix}] {verb} {len(affected)} 个")
    print(f"合计 {'已删除' if args.apply else '将删除'} {total} 个文件。" + ("" if args.apply else " 加 --apply 执行实删。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
