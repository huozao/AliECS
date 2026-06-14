from __future__ import annotations

from typing import Any

from config.settings import Settings, load_settings
from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.chanjet.pagination import paginate_query
from tplus_datahub.core.logger import get_logger


def sync_base_archive(
    *,
    module_name: str,
    endpoint: str,
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    query_params: dict[str, Any] | None = None,
) -> list[Any]:
    runtime_settings = settings or load_settings()
    runtime_client = client or ChanjetClient(runtime_settings)
    logger = get_logger(f"tplus_datahub.{module_name}")

    logger.info("Start syncing %s base archive", module_name)
    # /Query(V3.0) 实测完全支持 PageIndex/PageSize（PageSize=200→200、PageIndex=2→剩余189）。
    # 必须显式翻页，否则不传 PageSize 时只拿到服务端默认上限，数据增长后会静默截断（违反全量同步）。
    rows = paginate_query(
        client=runtime_client,
        endpoint=endpoint,
        module_name=module_name,
        settings=runtime_settings,
        base_payload=query_params,
        timestamp=timestamp,
    )
    logger.info("%s base archive sync finished: rows=%s", module_name, len(rows))
    return rows
