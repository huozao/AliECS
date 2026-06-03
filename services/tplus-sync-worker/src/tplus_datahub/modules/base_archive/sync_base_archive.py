from __future__ import annotations

from typing import Any

from config.settings import Settings, load_settings
from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.chanjet.response_parser import extract_rows
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp
from tplus_datahub.storage.raw_writer import save_raw_response


def sync_base_archive(
    *,
    module_name: str,
    endpoint: str,
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    query_params: dict[str, Any] | None = None,
) -> list[Any]:
    runtime_settings = settings or load_settings()
    runtime_client = client or ChanjetClient(runtime_settings)
    logger = get_logger(f"tplus_datahub.{module_name}")
    run_timestamp = timestamp or now_timestamp()

    payload = {"param": dict(query_params or {})}
    logger.info("Start syncing %s base archive", module_name)
    response = runtime_client.post(endpoint, payload)
    save_raw_response(module_name, 1, response, runtime_settings.data_root, run_timestamp)
    rows = extract_rows(response)
    logger.info("%s base archive sync finished: rows=%s", module_name, len(rows))
    return rows
