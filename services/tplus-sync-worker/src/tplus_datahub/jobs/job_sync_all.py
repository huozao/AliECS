from __future__ import annotations

from tplus_datahub.core.logger import get_logger
from tplus_datahub.jobs.job_sync_bom import main as sync_bom_main


PENDING_MODULES = ["material", "product", "purchase_price", "sales_price", "cost"]


def main() -> int:
    logger = get_logger("tplus_datahub.job_sync_all", "output/logs/job_sync_all.log")
    result = sync_bom_main()
    for module_name in PENDING_MODULES:
        logger.info("%s 模块接口待确认，本次跳过", module_name)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
