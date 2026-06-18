from __future__ import annotations

from pathlib import Path
from typing import Any

from config.settings import Settings, load_settings
from tplus_datahub.core.utils import ensure_dir, now_timestamp
from tplus_datahub.modules.bom.transform_bom import CHILD_COLUMNS, PARENT_COLUMNS, transform_bom_workbook_rows
from tplus_datahub.storage.retention import prune_exports


def export_bom(rows: list[Any], settings: Settings | None = None, timestamp: str | None = None) -> Path:
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise ImportError("缺少 pandas/openpyxl，请先运行 pip install -r requirements.txt") from exc

    runtime_settings = settings or load_settings()
    run_timestamp = timestamp or now_timestamp()
    target_dir = ensure_dir(Path(runtime_settings.output_root) / "excel")
    target = target_dir / f"bom_{run_timestamp}.xlsx"
    parent_rows, child_rows = transform_bom_workbook_rows(rows)

    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        pd.DataFrame(parent_rows, columns=PARENT_COLUMNS).to_excel(writer, sheet_name="物料清单", index=False)
        pd.DataFrame(child_rows, columns=CHILD_COLUMNS).to_excel(writer, sheet_name="子件明细", index=False)

    prune_exports(target_dir, "bom")
    return target
