from __future__ import annotations

from typing import Any

from config.settings import Settings
from tplus_datahub.modules.price_common import sync_price_report


def sync_sales_price(
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    begin_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    return sync_price_report(
        module_name="sales_price",
        settings=settings,
        client=client,
        timestamp=timestamp,
        begin_date=begin_date,
        end_date=end_date,
    )
