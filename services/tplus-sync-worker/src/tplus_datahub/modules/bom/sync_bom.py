from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from config.endpoints import BOM_QUERY_PAGE
from config.settings import Settings, load_settings
from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.chanjet.pagination import paginate_query
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp


DEFAULT_DISABLED_FILTERS = (("0", "enabled"), ("1", "disabled"))


def sync_bom(
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    query_params: dict[str, Any] | None = None,
) -> list[Any]:
    runtime_settings = settings or load_settings()
    runtime_client = client or ChanjetClient(runtime_settings)
    logger = get_logger("tplus_datahub.bom")

    logger.info("Start syncing BOM data")

    if query_params is not None:
        rows = paginate_query(
            client=runtime_client,
            endpoint=BOM_QUERY_PAGE,
            module_name="bom",
            settings=runtime_settings,
            base_payload=query_params,
            timestamp=timestamp,
        )
        logger.info("BOM sync finished: rows=%s", len(rows))
        return rows

    run_timestamp = timestamp or now_timestamp()
    rows_all: list[Any] = []
    for disabled_value, suffix in DEFAULT_DISABLED_FILTERS:
        rows_all.extend(
            paginate_query(
                client=runtime_client,
                endpoint=BOM_QUERY_PAGE,
                module_name="bom",
                settings=runtime_settings,
                base_payload={"Disabled": disabled_value},
                timestamp=f"{run_timestamp}_{suffix}",
            )
        )

    rows = _dedupe_bom_rows(rows_all)
    logger.info("BOM sync finished: rows=%s", len(rows))
    return rows


def _dedupe_bom_rows(rows: list[Any]) -> list[Any]:
    seen: set[tuple[Any, ...]] = set()
    deduped: list[Any] = []
    for row in rows:
        key = _bom_row_key(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _bom_row_key(row: Any) -> tuple[Any, ...]:
    if isinstance(row, Mapping):
        key = (row.get("ID"), row.get("Code"), row.get("Version"), row.get("Disabled"))
        if any(value is not None for value in key):
            return key
    return ("raw", repr(row))
