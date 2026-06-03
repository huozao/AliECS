from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from config.settings import Settings, load_settings
from tplus_datahub.storage.excel_writer import export_rows_to_excel


def export_base_archive(
    module_name: str,
    rows: list[Any],
    *,
    settings: Settings | None = None,
    timestamp: str | None = None,
):
    runtime_settings = settings or load_settings()
    normalized_rows = [dict(row) if isinstance(row, Mapping) else {"value": row} for row in rows]
    return export_rows_to_excel(normalized_rows, module_name, runtime_settings.output_root, timestamp)
