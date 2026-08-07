from __future__ import annotations

from config.endpoints import VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS, VERIFIED_VOUCHER_LIST_ENDPOINTS
from config.settings import ConfigError, load_settings
from tplus_datahub.core.exceptions import ChanjetAPIError, TPlusDataHubError
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp, text_preview
from tplus_datahub.jobs.sync_state import persist_inventory_records, upsert_and_snapshot_full_bom
from tplus_datahub.modules.base_archive.export_base_archive import export_base_archive
from tplus_datahub.modules.base_archive.sync_base_archive import sync_base_archive
from tplus_datahub.modules.bom.export_bom import export_bom
from tplus_datahub.modules.bom.sync_bom import sync_bom
from tplus_datahub.modules.inventory.export_stock import export_inventory
from tplus_datahub.modules.inventory.sync_stock import sync_inventory
from tplus_datahub.modules.partner.export_partner import export_partner
from tplus_datahub.modules.partner.sync_partner import sync_partner
from tplus_datahub.modules.purchase_price.export_purchase_price import export_purchase_price
from tplus_datahub.modules.purchase_price.sync_purchase_price import sync_purchase_price
from tplus_datahub.modules.sales_price.export_sales_price import export_sales_price
from tplus_datahub.modules.sales_price.sync_sales_price import sync_sales_price
from tplus_datahub.modules.voucher.export_voucher_list import export_voucher_list
from tplus_datahub.modules.voucher.sync_voucher_list import sync_voucher_list

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SyncAllResult:
    exit_code: int
    export_files: list[str] = field(default_factory=list)
    diff_summary: dict | None = None
    full_snapshot_id: int | None = None


def _basename(path: object) -> str:
    return Path(str(path)).name


PENDING_MODULES = [
    "material",
    "product",
    "cost",
    "department",
    "person",
    "marketing_organ",
    "settle_style",
    "bank_account",
    "currency",
    "expense",
    "income",
    "sales",
    "purchase",
]


def run() -> SyncAllResult:
    logger = get_logger("tplus_datahub.job_sync_all", "output/logs/job_sync_all.log")
    timestamp = now_timestamp()
    exports: list[str] = []
    try:
        settings = load_settings()

        bom_rows = sync_bom(settings=settings, timestamp=timestamp)
        snap = upsert_and_snapshot_full_bom(bom_rows, mode="scheduled_full", source_json={"job": "job_sync_all"})
        bom_path = export_bom(snap.full_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(bom_path)); logger.info("BOM Excel exported: %s", bom_path)

        inventory_rows = sync_inventory(settings=settings, timestamp=timestamp)
        inventory_path = export_inventory(inventory_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(inventory_path)); logger.info("Inventory Excel exported: %s", inventory_path)
        persist_inventory_records(inventory_rows, mode="scheduled_full")

        partner_rows = sync_partner(settings=settings, timestamp=timestamp)
        partner_path = export_partner(partner_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(partner_path)); logger.info("Partner Excel exported: %s", partner_path)

        for module_name, endpoint in VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS.items():
            archive_rows = sync_base_archive(module_name=module_name, endpoint=endpoint, settings=settings, timestamp=timestamp)
            archive_path = export_base_archive(module_name, archive_rows, settings=settings, timestamp=timestamp)
            exports.append(_basename(archive_path)); logger.info("%s Excel exported: %s", module_name, archive_path)

        for module_name, config in VERIFIED_VOUCHER_LIST_ENDPOINTS.items():
            voucher_rows = sync_voucher_list(module_name=module_name, endpoint=config["endpoint"], select_fields=config["select_fields"], settings=settings, timestamp=timestamp)
            voucher_path = export_voucher_list(module_name, voucher_rows, settings=settings, timestamp=timestamp)
            exports.append(_basename(voucher_path)); logger.info("%s Excel exported: %s", module_name, voucher_path)

        purchase_price_rows = sync_purchase_price(settings=settings, timestamp=timestamp)
        purchase_price_path = export_purchase_price(purchase_price_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(purchase_price_path)); logger.info("purchase_price Excel exported: %s", purchase_price_path)

        sales_price_rows = sync_sales_price(settings=settings, timestamp=timestamp)
        sales_price_path = export_sales_price(sales_price_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(sales_price_path)); logger.info("sales_price Excel exported: %s", sales_price_path)

        for module_name in PENDING_MODULES:
            logger.info("%s module endpoint is not confirmed; skipped", module_name)
        return SyncAllResult(0, exports, diff_summary=snap.diff_summary, full_snapshot_id=snap.full_snapshot_id)
    except ConfigError as exc:
        logger.error("Config error: %s", exc); return SyncAllResult(2, exports)
    except ChanjetAPIError as exc:
        logger.error("API error: endpoint=%s status=%s body=%s", exc.endpoint, exc.status_code, text_preview(exc.body_preview)); return SyncAllResult(3, exports)
    except TPlusDataHubError as exc:
        logger.error("Sync failed: %s", exc); return SyncAllResult(4, exports)
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc); return SyncAllResult(1, exports)


def main() -> int:
    return run().exit_code


if __name__ == "__main__":
    raise SystemExit(main())
