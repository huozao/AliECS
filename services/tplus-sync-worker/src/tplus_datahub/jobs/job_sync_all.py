from __future__ import annotations

from config.settings import ConfigError, load_settings
from tplus_datahub.core.exceptions import ChanjetAPIError, TPlusDataHubError
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp, text_preview
from tplus_datahub.modules.bom.export_bom import export_bom
from tplus_datahub.modules.bom.sync_bom import sync_bom
from tplus_datahub.modules.inventory.export_stock import export_inventory
from tplus_datahub.modules.inventory.sync_stock import sync_inventory
from tplus_datahub.modules.partner.export_partner import export_partner
from tplus_datahub.modules.partner.sync_partner import sync_partner


PENDING_MODULES = ["material", "product", "purchase_price", "sales_price", "cost", "warehouse", "sales", "purchase"]


def main() -> int:
    logger = get_logger("tplus_datahub.job_sync_all", "output/logs/job_sync_all.log")
    timestamp = now_timestamp()

    try:
        settings = load_settings()

        bom_rows = sync_bom(settings=settings, timestamp=timestamp)
        bom_path = export_bom(bom_rows, settings=settings, timestamp=timestamp)
        logger.info("BOM Excel exported: %s", bom_path)

        inventory_rows = sync_inventory(settings=settings, timestamp=timestamp)
        inventory_path = export_inventory(inventory_rows, settings=settings, timestamp=timestamp)
        logger.info("Inventory Excel exported: %s", inventory_path)

        partner_rows = sync_partner(settings=settings, timestamp=timestamp)
        partner_path = export_partner(partner_rows, settings=settings, timestamp=timestamp)
        logger.info("Partner Excel exported: %s", partner_path)

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
