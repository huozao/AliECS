from __future__ import annotations

from config.settings import ConfigError, load_settings
from tplus_datahub.core.exceptions import ChanjetAPIError, TPlusDataHubError
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp, text_preview
from tplus_datahub.modules.sales_price.export_sales_price import export_sales_price
from tplus_datahub.modules.sales_price.sync_sales_price import sync_sales_price


def main() -> int:
    logger = get_logger("tplus_datahub.job_sync_sales_price", "output/logs/job_sync_sales_price.log")
    timestamp = now_timestamp()
    try:
        settings = load_settings()
        rows = sync_sales_price(settings=settings, timestamp=timestamp)
        excel_path = export_sales_price(rows, settings=settings, timestamp=timestamp)
        logger.info("Sales price Excel exported: %s", excel_path)
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
