from __future__ import annotations

import os
import re
from pathlib import Path

from tplus_datahub.core.logger import get_logger

# 每类导出（bom_*/purchase_price_*/inventory_* …）默认只保留 mtime 最新的 N 个，
# 其余旧文件在每次产出新文件后顺手清理，避免 /app/tplus-output/excel 无限增长、
# 拖慢运行时按前缀的 glob（backend-api 取 mtime 最新一份做配方/价格查询）。
DEFAULT_RETENTION_KEEP = 48
_RETENTION_ENV = "TPLUS_EXPORT_RETENTION"

logger = get_logger("tplus_datahub.retention")


def resolve_retention_keep(keep: int | None = None) -> int:
    """决定每类保留数量：显式入参优先，其次环境变量 TPLUS_EXPORT_RETENTION，最后默认值。下限 1。"""
    if keep is not None:
        return max(int(keep), 1)
    raw = os.getenv(_RETENTION_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_RETENTION_KEEP
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退默认保留 %d", _RETENTION_ENV, raw, DEFAULT_RETENTION_KEEP)
        return DEFAULT_RETENTION_KEEP
    return max(value, 1)


def prune_exports(
    directory: str | Path,
    prefix: str,
    *,
    keep: int | None = None,
    dry_run: bool = False,
) -> list[Path]:
    """保留 `directory` 下 `{prefix}_<时间戳>.xlsx` 中 mtime 最新的 keep 个，删除其余旧文件。

    只匹配本 worker 产出的「前缀 + 标准时间戳(%Y%m%d_%H%M%S)」文件，避免误删手工文件或
    前缀相互包含的相邻模块（如 unit 与 unit_group）。永不删除最新的那个（keep>=1 时它必在保
    留集合内，与运行时取 mtime 最新一致）。返回被删除（dry_run 时为「将删除」）的文件列表。
    """
    keep_count = resolve_retention_keep(keep)
    target_dir = Path(directory)
    if not target_dir.exists():
        return []

    pattern = re.compile(rf"^{re.escape(prefix)}_\d{{8}}_\d{{6}}\.xlsx$")
    candidates = [
        path
        for path in target_dir.glob(f"{prefix}_*.xlsx")
        if path.is_file() and not path.name.startswith("~$") and pattern.match(path.name)
    ]
    if len(candidates) <= keep_count:
        return []

    # 与运行时「取 mtime 最新」一致：按 (mtime, 文件名) 降序，保留前 keep 个。
    ordered = sorted(candidates, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    stale = ordered[keep_count:]

    logger.info(
        "保留策略[%s_*]：共 %d 个，保留最新 %d，%s %d 个：%s",
        prefix,
        len(candidates),
        keep_count,
        "将删除(dry-run)" if dry_run else "删除",
        len(stale),
        ", ".join(p.name for p in stale),
    )
    if dry_run:
        return stale

    deleted: list[Path] = []
    for path in stale:
        try:
            path.unlink()
            deleted.append(path)
        except OSError as exc:  # 单个删除失败不影响主同步流程
            logger.warning("删除旧导出失败 %s：%s", path, exc)
    return deleted
