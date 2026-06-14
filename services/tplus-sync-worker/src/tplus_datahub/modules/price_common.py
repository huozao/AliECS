from __future__ import annotations

from typing import Any, Callable

from config.settings import Settings, load_settings
from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp
from tplus_datahub.modules.voucher.sync_voucher_list import _extract_success_data, _rows_to_dicts, _to_int
from tplus_datahub.storage.raw_writer import save_raw_response


PURCHASE_PRICE_COLUMNS = [
    "单据日期",
    "单据编号",
    "供应商编码",
    "供应商",
    "供应商简称",
    "部门",
    "业务员",
    "仓库",
    "项目",
    "存货编码",
    "存货",
    "规格型号",
    "计量单位",
    "数量",
    "折扣%",
    "单价",
    "金额",
    "税率%",
    "含税单价",
    "含税金额",
    "税额",
]

SALES_PRICE_COLUMNS = [
    "单据日期",
    "单据编号",
    "客户",
    "部门",
    "业务员",
    "存货编码",
    "存货",
    "规格型号",
    "计量单位",
    "数量",
    "折扣%",
    "单价",
    "金额",
    "含税单价",
    "含税金额",
    "税额",
]


def unwrap_dto(dto: Any) -> dict[str, Any]:
    if isinstance(dto, dict) and isinstance(dto.get("data"), dict):
        return dto["data"]
    return dto if isinstance(dto, dict) else {}


def iter_details(document: dict[str, Any]) -> list[dict[str, Any]]:
    details = pick(document, "Details", "details", "Detail", "detail")
    if not isinstance(details, list):
        return []
    return [item for item in details if isinstance(item, dict)]


def pick(source: Any, *paths: str) -> Any:
    for path in paths:
        current = source
        for key in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = _get_case_insensitive(current, key)
            if current in (None, ""):
                break
        if current not in (None, ""):
            return current
    return None


def display(value: Any) -> Any:
    if isinstance(value, dict):
        return pick(value, "Name", "name", "FullName", "fullName", "ShortName", "shortName")
    return value


def code(value: Any) -> Any:
    if isinstance(value, dict):
        return pick(value, "Code", "code", "ID", "id")
    return value


def number(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            parsed = float(text)
        except ValueError:
            return value
        return int(parsed) if parsed.is_integer() else parsed
    return value


def row_for_columns(columns: list[str], values: dict[str, Any]) -> dict[str, Any]:
    return {column: values.get(column) for column in columns}


def sync_price_rows(
    *,
    module_name: str,
    endpoint_config: dict[str, Any],
    transform: Callable[[list[Any]], list[dict[str, Any]]],
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
    voucher_rows: list[dict[str, Any]] = []

    logger.info("Start syncing %s voucher ids", module_name)
    while True:
        payload = {
            "pageSize": page_size,
            "pageIndex": page_index,
            "selectFields": list(endpoint_config["select_fields"]),
            "paramDic": dict(param_dic or {}),
        }
        response = _post_with_retries(runtime_client, endpoint_config["list_endpoint"], payload, logger)
        save_raw_response(f"{module_name}_voucher_list", page_index + 1, response, runtime_settings.data_root, run_timestamp)
        data = _extract_success_data(response, module_name)
        page_rows = _rows_to_dicts(data)
        voucher_rows.extend(page_rows)

        total_pages = _to_int(data.get("TotalPageNum"), default=0)
        if not page_rows or page_index + 1 >= total_pages:
            break
        page_index += 1

    detail_responses: list[Any] = []
    for index, voucher_row in enumerate(voucher_rows, start=1):
        voucher_id = _voucher_id(voucher_row)
        if not voucher_id:
            logger.warning("%s voucher row missing ID; skipped: %s", module_name, voucher_row)
            continue
        # Implementation note: GetVoucherDTO payload shape is based on prior local research.
        # Verify against the exact Chanjet T+ tenant/version during the realtime validation step.
        response = _post_with_retries(runtime_client, endpoint_config["detail_endpoint"], {"id": voucher_id}, logger)
        save_raw_response(f"{module_name}_dto", index, response, runtime_settings.data_root, run_timestamp)
        detail_responses.append(response)

    rows = transform(detail_responses)
    logger.info("%s price sync finished: vouchers=%s rows=%s", module_name, len(voucher_rows), len(rows))
    return rows


def _post_with_retries(client: Any, endpoint: str, payload: dict[str, Any], logger: Any, *, attempts: int = 3) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.post(endpoint, payload)
        except Exception as exc:  # noqa: PERF203 - retry boundary for external API calls
            last_exc = exc
            logger.warning("T+ request failed attempt %s/%s endpoint=%s error=%s", attempt, attempts, endpoint, exc)
    assert last_exc is not None
    raise last_exc


def _voucher_id(row: dict[str, Any]) -> Any:
    for key, value in row.items():
        normalized = key.lower()
        if normalized == "id" or normalized.endswith(".id"):
            return value
    return pick(row, "ID", "id")


def _get_case_insensitive(mapping: dict[str, Any], key: str) -> Any:
    if key in mapping:
        return mapping[key]
    lowered = key.lower()
    for candidate, value in mapping.items():
        if candidate.lower() == lowered:
            return value
    return None
