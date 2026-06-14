from __future__ import annotations

from typing import Any

from config.endpoints import VERIFIED_PRICE_REPORTS
from tplus_datahub.modules.price_common import map_report_rows


def transform_purchase_price_rows(responses: list[Any]) -> list[dict[str, Any]]:
    """把 GetReportData(PU_PurchaseArrivalDetailRpt) 的响应映射成采购价格导出行。"""
    return map_report_rows(responses, VERIFIED_PRICE_REPORTS["purchase_price"]["columns"])
