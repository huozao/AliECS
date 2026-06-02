from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tplus_datahub.core.utils import ensure_dir


def write_json(data: Any, path: str | Path) -> Path:
    target = Path(path)
    ensure_dir(target.parent)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
