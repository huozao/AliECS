from __future__ import annotations

from typing import Any

from config.endpoints import BOM_QUERY_PAGE
from config.settings import Settings, load_settings
from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.chanjet.pagination import paginate_query
from tplus_datahub.core.logger import get_logger


def sync_bom(
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    query_params: dict[str, Any] | None = None,
) -> list[Any]:
    runtime_settings = settings or load_settings()
    runtime_client = client or ChanjetClient(runtime_settings)
    logger = get_logger("tplus_datahub.bom")

    logger.info("开始同步 BOM 数据")
    rows = paginate_query(
        client=runtime_client,
        endpoint=BOM_QUERY_PAGE,
        module_name="bom",
        settings=runtime_settings,
        base_payload=query_params or {},
        timestamp=timestamp,
    )
    logger.info("BOM 同步完成，共 %s 条", len(rows))
    return rows
