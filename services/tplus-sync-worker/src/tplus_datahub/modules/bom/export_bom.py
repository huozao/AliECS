from __future__ import annotations

from pathlib import Path
from typing import Any

from config.settings import Settings, load_settings
from tplus_datahub.modules.bom.transform_bom import transform_bom_rows
from tplus_datahub.storage.excel_writer import export_rows_to_excel


def export_bom(rows: list[Any], settings: Settings | None = None, timestamp: str | None = None) -> Path:
    runtime_settings = settings or load_settings()
    flattened_rows = transform_bom_rows(rows)
    return export_rows_to_excel(flattened_rows, "bom", runtime_settings.output_root, timestamp)
