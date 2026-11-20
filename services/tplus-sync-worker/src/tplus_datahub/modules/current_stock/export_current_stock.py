from __future__ import annotations

from pathlib import Path
from typing import Any

from config.settings import Settings, load_settings
from tplus_datahub.core.logger import get_logger
from tplus_datahub.storage.excel_writer import export_rows_to_excel


def export_current_stock(
    rows: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
    timestamp: str | None = None,
) -> Path | None:
    """把现存量数据导出到 Excel 文件。若 rows 为空则拒绝覆盖已有数据。"""
    logger = get_logger("tplus_datahub.current_stock")
    runtime_settings = settings or load_settings()

    if not rows:
        logger.warning("export_current_stock: rows is empty, skipping export to protect existing stock file.")
        return None

    path = export_rows_to_excel(rows, "current_stock", runtime_settings.output_root, timestamp)
    logger.info("Current stock exported to: %s (rows=%d)", path, len(rows))
    return path
