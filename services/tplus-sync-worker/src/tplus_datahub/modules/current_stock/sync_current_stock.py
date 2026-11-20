from __future__ import annotations

import logging
from typing import Any

from config.endpoints import REPORT_QUERY_ENDPOINT
from config.settings import Settings, load_settings
from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp
from tplus_datahub.modules.price_common import number
from tplus_datahub.storage.raw_writer import save_raw_response

REPORT_NAME = "ST_CurrentStockRpt"
DETAIL_ROW_TYPE = "D"


def sync_current_stock(
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    inventory_class_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """通过 T+ 官方报表引擎拉取现存量报表，包含账面现存量、可用量、生产待发占用量等全量指标。"""
    runtime_settings = settings or load_settings()
    runtime_client = client or ChanjetClient(runtime_settings)
    run_timestamp = timestamp or now_timestamp()
    logger = get_logger("tplus_datahub.current_stock")

    logger.info("Start syncing current_stock report via %s", REPORT_NAME)
    page_size = 200
    page_index = 1
    task_session_id: Any = None
    solution_id: Any = None
    all_rows: list[dict[str, Any]] = []

    while True:
        request_body: dict[str, Any] = {
            "ReportName": REPORT_NAME,
            "PageIndex": page_index,
            "PageSize": page_size,
            "SearchItems": [],
            "ReportTableColNames": "",
        }
        if task_session_id:
            request_body["TaskSessionID"] = task_session_id
        if solution_id:
            request_body["SolutionID"] = solution_id

        try:
            response = runtime_client.post(REPORT_QUERY_ENDPOINT, {"request": request_body})
        except Exception as exc:
            logger.error("Failed to query %s on page %s: %s", REPORT_NAME, page_index, exc)
            raise

        save_raw_response("current_stock_report", page_index, response, runtime_settings.data_root, run_timestamp)

        if not isinstance(response, dict):
            break

        status = response.get("Status")
        if status != 0:
            err_msg = response.get("ErrorMessage") or f"Status {status}"
            logger.error("T+ report error for %s: %s", REPORT_NAME, err_msg)
            break

        task_session_id = response.get("TaskSessionID") or task_session_id
        solution_id = response.get("SolutionID") or solution_id
        total_pages = int(response.get("Pages") or 0)

        data_source = response.get("DataSource") or {}
        raw_rows = data_source.get("Rows") or []
        detail_rows = [r for r in raw_rows if isinstance(r, dict) and r.get("rowType") == DETAIL_ROW_TYPE]
        logger.info("Page %d/%d: got %d detail rows", page_index, max(total_pages, 1), len(detail_rows))

        for r in detail_rows:
            inv_code = str(r.get("InventoryCode") or "").strip()
            if not inv_code:
                continue

            inv_name = str(r.get("Inventory") or "").strip()
            spec = str(r.get("Specification") or "").strip()
            unit_name = str(r.get("BaseUnit") or "").strip()

            wh_code = str(r.get("WarehouseCode") or "").strip() or "001"
            wh_name = str(r.get("Warehouse") or "").strip() or "原材库"

            raw_base_qty = number(r.get("BaseQuantity")) or 0.0
            raw_avail_qty = number(r.get("canuseBaseQuantity")) or 0.0
            raw_dispatch_qty = number(r.get("producefordispatchbasequantity")) or 0.0
            raw_onway_qty = (
                number(r.get("[PurchaseOnWayHeadColumn-采购在途量][purchaseorderonwayBaseQuantity-订单在途量]"))
                or number(r.get("purchaseorderonwayBaseQuantity"))
                or 0.0
            )
            raw_produce_onway = number(r.get("onproducingbasequantity")) or 0.0

            # 现存量：若有账面物理现存量则取账面量；若账面为0但有生产待发，则以生产待发量代表实际在库原料
            existing_qty = float(raw_base_qty if raw_base_qty > 0 else (raw_dispatch_qty if raw_dispatch_qty > 0 else 0.0))
            avail_qty = float(raw_avail_qty)
            dispatch_qty = float(raw_dispatch_qty)
            onway_qty = float(raw_onway_qty)

            # 存货分类映射
            class_name = ""
            if inventory_class_map and inv_code in inventory_class_map:
                class_name = inventory_class_map[inv_code]
            elif inv_code.startswith(("01", "10", "20", "30", "02", "03", "04")):
                class_name = "原材料"

            all_rows.append({
                "WarehouseCode": wh_code,
                "WarehouseName": wh_name,
                "InventoryCode": inv_code,
                "InventoryName": inv_name,
                "InventoryClassName": class_name,
                "Specification": spec,
                "UnitName": unit_name,
                "ExistingQuantity": existing_qty,
                "AvailableQuantity": avail_qty,
                "ProduceDispatchQuantity": dispatch_qty,
                "PurchaseOnWayQuantity": onway_qty,
                "ProduceOnWayQuantity": float(raw_produce_onway),
            })

        if page_index >= total_pages or not detail_rows:
            break
        page_index += 1

    logger.info("Current stock report sync finished: total_rows=%d", len(all_rows))
    return all_rows
