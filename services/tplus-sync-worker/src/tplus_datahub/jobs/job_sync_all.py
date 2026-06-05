from __future__ import annotations

from config.endpoints import VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS, VERIFIED_VOUCHER_LIST_ENDPOINTS
from config.settings import ConfigError, load_settings
from tplus_datahub.core.exceptions import ChanjetAPIError, TPlusDataHubError
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp, text_preview
from tplus_datahub.jobs.sync_state import record_bom_snapshot_if_configured
from tplus_datahub.modules.base_archive.export_base_archive import export_base_archive
from tplus_datahub.modules.base_archive.sync_base_archive import sync_base_archive
from tplus_datahub.modules.bom.export_bom import export_bom
from tplus_datahub.modules.bom.sync_bom import sync_bom
from tplus_datahub.modules.inventory.export_stock import export_inventory
from tplus_datahub.modules.inventory.sync_stock import sync_inventory
from tplus_datahub.modules.partner.export_partner import export_partner
from tplus_datahub.modules.partner.sync_partner import sync_partner
from tplus_datahub.modules.voucher.export_voucher_list import export_voucher_list
from tplus_datahub.modules.voucher.sync_voucher_list import sync_voucher_list


PENDING_MODULES = [
    "material",
    "product",
    "purchase_price",
    "sales_price",
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


def main() -> int:
    logger = get_logger("tplus_datahub.job_sync_all", "output/logs/job_sync_all.log")
    timestamp = now_timestamp()

    try:
        settings = load_settings()

        bom_rows = sync_bom(settings=settings, timestamp=timestamp)
        bom_path = export_bom(bom_rows, settings=settings, timestamp=timestamp)
        record_bom_snapshot_if_configured(bom_rows, mode="scheduled_full", source_json={"job": "job_sync_all"})
        logger.info("BOM Excel exported: %s", bom_path)

        inventory_rows = sync_inventory(settings=settings, timestamp=timestamp)
        inventory_path = export_inventory(inventory_rows, settings=settings, timestamp=timestamp)
        logger.info("Inventory Excel exported: %s", inventory_path)

        partner_rows = sync_partner(settings=settings, timestamp=timestamp)
        partner_path = export_partner(partner_rows, settings=settings, timestamp=timestamp)
        logger.info("Partner Excel exported: %s", partner_path)

        for module_name, endpoint in VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS.items():
            archive_rows = sync_base_archive(
                module_name=module_name,
                endpoint=endpoint,
                settings=settings,
                timestamp=timestamp,
            )
            archive_path = export_base_archive(module_name, archive_rows, settings=settings, timestamp=timestamp)
            logger.info("%s Excel exported: %s", module_name, archive_path)

        for module_name, config in VERIFIED_VOUCHER_LIST_ENDPOINTS.items():
            voucher_rows = sync_voucher_list(
                module_name=module_name,
                endpoint=config["endpoint"],
                select_fields=config["select_fields"],
                settings=settings,
                timestamp=timestamp,
            )
            voucher_path = export_voucher_list(module_name, voucher_rows, settings=settings, timestamp=timestamp)
            logger.info("%s Excel exported: %s", module_name, voucher_path)

        for module_name in PENDING_MODULES:
            logger.info("%s module endpoint is not confirmed; skipped", module_name)
        return 0
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        return 2
    except ChanjetAPIError as exc:
        logger.error("API error: endpoint=%s status=%s body=%s", exc.endpoint, exc.status_code, text_preview(exc.body_preview))
        return 3
    except TPlusDataHubError as exc:
        logger.error("Sync failed: %s", exc)
        return 4
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
