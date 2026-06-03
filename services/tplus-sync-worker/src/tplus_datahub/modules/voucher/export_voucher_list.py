from __future__ import annotations

from typing import Any

from config.settings import Settings, load_settings
from tplus_datahub.storage.excel_writer import export_rows_to_excel


def export_voucher_list(
    module_name: str,
    rows: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
    timestamp: str | None = None,
):
    runtime_settings = settings or load_settings()
    return export_rows_to_excel(rows, module_name, runtime_settings.output_root, timestamp)
