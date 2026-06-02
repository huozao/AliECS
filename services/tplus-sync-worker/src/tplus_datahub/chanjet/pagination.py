from __future__ import annotations

from typing import Any

from config.settings import Settings
from tplus_datahub.chanjet.response_parser import extract_rows
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp
from tplus_datahub.storage.raw_writer import save_raw_response


def paginate_query(
    *,
    client: Any,
    endpoint: str,
    module_name: str,
    settings: Settings,
    base_payload: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> list[Any]:
    logger = get_logger(f"tplus_datahub.{module_name}")
    page_size = settings.default_page_size
    page_index = 1
    rows_all: list[Any] = []
    run_timestamp = timestamp or now_timestamp()

    while True:
        param = dict(base_payload or {})
        param["PageIndex"] = page_index
        param["PageSize"] = page_size
        payload = {"param": param}

        response = client.post(endpoint, payload)
        save_raw_response(module_name, page_index, response, settings.data_root, run_timestamp)
        rows = extract_rows(response)
        logger.info("第 %s 页：获取 %s 条", page_index, len(rows))

        if not rows:
            break

        rows_all.extend(rows)
        if len(rows) < page_size:
            break
        page_index += 1

    return rows_all
