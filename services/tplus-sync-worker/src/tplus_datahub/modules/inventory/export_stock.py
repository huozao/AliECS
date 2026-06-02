from __future__ import annotations

from typing import Any

from config.settings import Settings, load_settings
from tplus_datahub.modules.inventory.transform_stock import transform_inventory_rows
from tplus_datahub.storage.excel_writer import export_rows_to_excel


def export_inventory(rows: list[Any], *, settings: Settings | None = None, timestamp: str | None = None):
    runtime_settings = settings or load_settings()
    flattened_rows = transform_inventory_rows(rows)
    return export_rows_to_excel(flattened_rows, "inventory", runtime_settings.output_root, timestamp)


def export_stock(rows: list[Any], *, settings: Settings | None = None, timestamp: str | None = None):
    return export_inventory(rows, settings=settings, timestamp=timestamp)
