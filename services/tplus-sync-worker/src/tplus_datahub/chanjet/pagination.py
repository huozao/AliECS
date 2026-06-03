from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from config.settings import Settings
from tplus_datahub.chanjet.response_parser import extract_rows
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp
from tplus_datahub.storage.raw_writer import save_raw_response


DEFAULT_DISABLED_FILTERS = (("0", "enabled", "False"), ("1", "disabled", "True"))


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


def paginate_query_disabled_variants(
    *,
    client: Any,
    endpoint: str,
    module_name: str,
    settings: Settings,
    timestamp: str | None = None,
    annotate_missing_disabled: bool = False,
    dedupe_key_fields: tuple[str, ...] = ("ID", "Code"),
) -> list[Any]:
    run_timestamp = timestamp or now_timestamp()
    rows_all: list[Any] = []

    for disabled_value, suffix, disabled_label in DEFAULT_DISABLED_FILTERS:
        rows = paginate_query(
            client=client,
            endpoint=endpoint,
            module_name=module_name,
            settings=settings,
            base_payload={"Disabled": disabled_value},
            timestamp=f"{run_timestamp}_{suffix}",
        )
        if annotate_missing_disabled:
            rows = [_with_disabled_status(row, disabled_label) for row in rows]
        rows_all.extend(rows)

    return _dedupe_rows(rows_all, dedupe_key_fields)


def _with_disabled_status(row: Any, disabled_label: str) -> Any:
    if not isinstance(row, Mapping):
        return row
    copied = dict(row)
    if copied.get("Disabled") in (None, ""):
        copied["Disabled"] = disabled_label
    return copied


def _dedupe_rows(rows: list[Any], key_fields: tuple[str, ...]) -> list[Any]:
    seen: set[tuple[Any, ...]] = set()
    deduped: list[Any] = []
    for row in rows:
        key = _row_key(row, key_fields)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _row_key(row: Any, key_fields: tuple[str, ...]) -> tuple[Any, ...]:
    if isinstance(row, Mapping):
        values = tuple(row.get(field) for field in key_fields)
        if any(value not in (None, "") for value in values):
            return ("fields", *values)
    return ("raw", repr(row))
