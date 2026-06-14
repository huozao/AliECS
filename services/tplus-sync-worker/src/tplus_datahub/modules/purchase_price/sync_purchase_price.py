from __future__ import annotations

from typing import Any

from config.endpoints import VERIFIED_PRICE_ENDPOINTS
from config.settings import Settings
from tplus_datahub.modules.price_common import sync_price_rows
from tplus_datahub.modules.purchase_price.transform_purchase_price import transform_purchase_price_rows


def sync_purchase_price(
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    param_dic: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return sync_price_rows(
        module_name="purchase_price",
        endpoint_config=VERIFIED_PRICE_ENDPOINTS["purchase_price"],
        transform=transform_purchase_price_rows,
        settings=settings,
        client=client,
        timestamp=timestamp,
        param_dic=param_dic,
    )
