from __future__ import annotations

from typing import Any

from config.endpoints import INVENTORY_QUERY_PAGE
from config.settings import Settings, load_settings
from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.chanjet.pagination import paginate_query, paginate_query_disabled_variants
from tplus_datahub.core.logger import get_logger


def sync_inventory(
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    query_params: dict[str, Any] | None = None,
) -> list[Any]:
    runtime_settings = settings or load_settings()
    runtime_client = client or ChanjetClient(runtime_settings)
    logger = get_logger("tplus_datahub.inventory")

    logger.info("Start syncing inventory")
    if query_params is not None:
        rows = paginate_query(
            client=runtime_client,
            endpoint=INVENTORY_QUERY_PAGE,
            module_name="inventory",
            settings=runtime_settings,
            base_payload=query_params,
            timestamp=timestamp,
        )
    else:
        rows = paginate_query_disabled_variants(
            client=runtime_client,
            endpoint=INVENTORY_QUERY_PAGE,
            module_name="inventory",
            settings=runtime_settings,
            timestamp=timestamp,
            annotate_missing_disabled=True,
            extra_payload={"SelectFields": "ID,Code,Name,InventoryClass.Code,InventoryClass.Name,BaseUnitCode,BaseUnitName,Disabled"},
        )
    logger.info("Inventory sync finished: rows=%s", len(rows))
    return rows


def sync_stock(*args, **kwargs):
    return sync_inventory(*args, **kwargs)
