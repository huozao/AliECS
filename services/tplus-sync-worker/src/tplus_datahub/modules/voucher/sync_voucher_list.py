from __future__ import annotations

from typing import Any

from config.settings import Settings, load_settings
from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.core.exceptions import TPlusDataHubError
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp
from tplus_datahub.storage.raw_writer import save_raw_response


def sync_voucher_list(
    *,
    module_name: str,
    endpoint: str,
    select_fields: list[str],
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    param_dic: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    runtime_settings = settings or load_settings()
    runtime_client = client or ChanjetClient(runtime_settings)
    logger = get_logger(f"tplus_datahub.{module_name}")
    run_timestamp = timestamp or now_timestamp()
    page_size = runtime_settings.default_page_size
    page_index = 0
    rows_all: list[dict[str, Any]] = []

    logger.info("Start syncing %s voucher list", module_name)
    while True:
        payload = {
            "pageSize": page_size,
            "pageIndex": page_index,
            "selectFields": list(select_fields),
            "paramDic": dict(param_dic or {}),
        }
        response = runtime_client.post(endpoint, payload)
        save_raw_response(module_name, page_index + 1, response, runtime_settings.data_root, run_timestamp)

        data = _extract_success_data(response, module_name)
        page_rows = _rows_to_dicts(data)
        logger.info("%s voucher list page %s: rows=%s", module_name, page_index, len(page_rows))
        rows_all.extend(page_rows)

        total_pages = _to_int(data.get("TotalPageNum"), default=0)
        if not page_rows or page_index + 1 >= total_pages:
            break
        page_index += 1

    logger.info("%s voucher list sync finished: rows=%s", module_name, len(rows_all))
    return rows_all


def _extract_success_data(response: Any, module_name: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise TPlusDataHubError(f"{module_name} voucher list response is not a JSON object")
    code = response.get("code")
    if code != "0":
        raise TPlusDataHubError(f"{module_name} voucher list returned code={code} message={response.get('message')}")
    data = response.get("data")
    if not isinstance(data, dict):
        raise TPlusDataHubError(f"{module_name} voucher list response missing data object")
    return data


def _rows_to_dicts(data: dict[str, Any]) -> list[dict[str, Any]]:
    columns = data.get("Columns")
    rows = data.get("Rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, list):
            result.append({str(column): row[index] if index < len(row) else None for index, column in enumerate(columns)})
        else:
            result.append({"value": row})
    return result


def _to_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
