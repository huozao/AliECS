from __future__ import annotations

from typing import Any

from config.endpoints import PARTNER_QUERY_PAGE
from config.settings import Settings, load_settings
from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.chanjet.pagination import paginate_query
from tplus_datahub.core.logger import get_logger


def sync_partner(
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    query_params: dict[str, Any] | None = None,
) -> list[Any]:
    runtime_settings = settings or load_settings()
    runtime_client = client or ChanjetClient(runtime_settings)
    logger = get_logger("tplus_datahub.partner")

    logger.info("Start syncing partners")
    rows = paginate_query(
        client=runtime_client,
        endpoint=PARTNER_QUERY_PAGE,
        module_name="partner",
        settings=runtime_settings,
        base_payload=query_params or {},
        timestamp=timestamp,
    )
    logger.info("Partner sync finished: rows=%s", len(rows))
    return rows
