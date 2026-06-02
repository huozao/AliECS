from __future__ import annotations

from tplus_datahub.core.exceptions import EndpointNotConfirmedError
from tplus_datahub.core.logger import get_logger


def run_pending_job(module_name: str) -> int:
    logger = get_logger(f"tplus_datahub.job_{module_name}")
    try:
        raise EndpointNotConfirmedError(f"{module_name} 模块接口尚未确认，请先在 docs/api_notes.md 中补充接口路径")
    except EndpointNotConfirmedError as exc:
        logger.error("%s", exc)
        return 5
