from __future__ import annotations

from typing import Any

from config.endpoints import VERIFIED_PRICE_REPORTS
from tplus_datahub.modules.price_common import map_report_rows


def transform_sales_price_rows(responses: list[Any]) -> list[dict[str, Any]]:
    """把 GetReportData(SA_SaleDeliveryDetailRpt) 的响应映射成销售价格导出行。"""
    return map_report_rows(responses, VERIFIED_PRICE_REPORTS["sales_price"]["columns"])
