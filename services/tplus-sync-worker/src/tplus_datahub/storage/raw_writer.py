from __future__ import annotations

from pathlib import Path
from typing import Any

from tplus_datahub.storage.json_writer import write_json


def save_raw_response(module_name: str, page_index: int, response: Any, data_root: str | Path, timestamp: str) -> Path:
    target = Path(data_root) / "raw" / module_name / f"{timestamp}_page_{page_index}.json"
    return write_json(response, target)
