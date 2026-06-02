from __future__ import annotations

from typing import Any

from config.settings import Settings, load_settings
from tplus_datahub.modules.partner.transform_partner import transform_partner_rows
from tplus_datahub.storage.excel_writer import export_rows_to_excel


def export_partner(rows: list[Any], *, settings: Settings | None = None, timestamp: str | None = None):
    runtime_settings = settings or load_settings()
    flattened_rows = transform_partner_rows(rows)
    return export_rows_to_excel(flattened_rows, "partner", runtime_settings.output_root, timestamp)
