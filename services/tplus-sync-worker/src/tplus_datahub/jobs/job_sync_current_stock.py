from __future__ import annotations

from config.settings import ConfigError, load_settings
from tplus_datahub.core.exceptions import ChanjetAPIError, TPlusDataHubError
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp, text_preview
from tplus_datahub.modules.current_stock.export_current_stock import export_current_stock
from tplus_datahub.modules.current_stock.sync_current_stock import sync_current_stock


def main() -> int:
    logger = get_logger("tplus_datahub.job_sync_current_stock", "output/logs/job_sync_current_stock.log")
    timestamp = now_timestamp()
    try:
        settings = load_settings()
        rows = sync_current_stock(settings=settings, timestamp=timestamp)
        excel_path = export_current_stock(rows, settings=settings, timestamp=timestamp)
        if excel_path:
            logger.info("Current stock Excel exported: %s (rows=%d)", excel_path, len(rows))
        else:
            logger.warning("Current stock sync completed but no Excel was written (rows=0).")
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
