from __future__ import annotations

from tplus_datahub.core.exceptions import EndpointNotConfirmedError


def raise_pending(module_name: str):
    raise EndpointNotConfirmedError(f"{module_name} 模块接口尚未确认，请先在 docs/api_notes.md 中补充接口路径")
