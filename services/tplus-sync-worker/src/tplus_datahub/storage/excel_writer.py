from __future__ import annotations

from pathlib import Path
from typing import Any

from tplus_datahub.core.utils import ensure_dir, now_timestamp
from tplus_datahub.storage.retention import prune_exports


def export_rows_to_excel(rows: list[dict[str, Any]], module_name: str, output_root: str | Path, timestamp: str | None = None) -> Path:
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise ImportError("缺少 pandas/openpyxl，请先运行 pip install -r requirements.txt") from exc

    run_timestamp = timestamp or now_timestamp()
    target_dir = ensure_dir(Path(output_root) / "excel")
    target = target_dir / f"{module_name}_{run_timestamp}.xlsx"
    dataframe = pd.json_normalize(rows) if rows else pd.DataFrame()
    dataframe.to_excel(target, index=False)
    prune_exports(target_dir, module_name)
    return target
