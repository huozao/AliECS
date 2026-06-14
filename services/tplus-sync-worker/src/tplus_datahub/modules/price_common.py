from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

from config.endpoints import (
    PRICE_NUMERIC_COLUMNS,
    PRICE_PERCENT_COLUMNS,
    REPORT_QUERY_ENDPOINT,
    VERIFIED_PRICE_REPORTS,
)
from config.settings import Settings, load_settings
from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.core.logger import get_logger
from tplus_datahub.core.utils import now_timestamp
from tplus_datahub.storage.raw_writer import save_raw_response

# 全量起始日：取足够早的下限（T+ Cloud 不存在更早账套数据），等价于“无下限/全量”。
# 实测 GetReportData 接受 1990/2000 起始且结果不变；可用 env PRICE_SYNC_BEGIN_DATE 覆盖。
DEFAULT_BEGIN_DATE = "2000-01-01"
DETAIL_ROW_TYPE = "D"


def number(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return value
        return int(parsed) if parsed.is_integer() else parsed
    return value


def percent(value: Any) -> Any:
    """T+ 返回折扣为小数（1.0000=100%）；转成百分数显示。"""
    parsed = number(value)
    if isinstance(parsed, (int, float)):
        result = round(parsed * 100, 4)
        return int(result) if float(result).is_integer() else result
    return parsed


def _detail_rows(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    data_source = response.get("DataSource") or response.get("dataSource") or {}
    rows = data_source.get("Rows") if isinstance(data_source, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("rowType") == DETAIL_ROW_TYPE]


def map_report_rows(responses: list[Any], columns: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """把 GetReportData 的明细行按 (中文表头, 接口字段) 映射成有序导出行。"""
    rows: list[dict[str, Any]] = []
    for response in responses:
        for source in _detail_rows(response):
            row: dict[str, Any] = {}
            for header, field in columns:
                value = source.get(field)
                if header in PRICE_PERCENT_COLUMNS:
                    value = percent(value)
                elif header in PRICE_NUMERIC_COLUMNS:
                    value = number(value)
                elif value == "":
                    value = None
                row[header] = value
            rows.append(row)
    return rows


def fetch_report_responses(
    *,
    module_name: str,
    report_config: dict[str, Any],
    settings: Settings,
    client: Any,
    timestamp: str,
    begin_date: str,
    end_date: str,
) -> list[Any]:
    logger = get_logger(f"tplus_datahub.{module_name}")
    column_names = ",".join(field for _, field in report_config["columns"])
    page_size = settings.default_page_size
    page_index = 1
    task_session_id: Any = None
    solution_id: Any = None
    responses: list[Any] = []

    logger.info(
        "Start report sync %s report=%s range=%s..%s",
        module_name,
        report_config["report_name"],
        begin_date,
        end_date,
    )
    while True:
        request: dict[str, Any] = {
            "ReportName": report_config["report_name"],
            "PageIndex": page_index,
            "PageSize": page_size,
            "SearchItems": [
                {
                    "ColumnName": report_config["date_column"],
                    "BeginDefault": begin_date,
                    "BeginDefaultText": begin_date,
                    "EndDefault": end_date,
                    "EndDefaultText": end_date,
                }
            ],
            "ReportTableColNames": column_names,
        }
        if task_session_id:
            request["TaskSessionID"] = task_session_id
        if solution_id:
            request["SolutionID"] = solution_id

        response = _post_with_retries(client, REPORT_QUERY_ENDPOINT, {"request": request}, logger)
        save_raw_response(f"{module_name}_report", page_index, response, settings.data_root, timestamp)
        responses.append(response)

        total_pages = 0
        if isinstance(response, dict):
            error_message = response.get("ErrorMessage")
            if error_message:
                logger.warning("%s report ErrorMessage on page %s: %s", module_name, page_index, error_message)
            task_session_id = response.get("TaskSessionID") or task_session_id
            solution_id = response.get("SolutionID") or solution_id
            total_pages = _to_int(response.get("Pages"))

        if not _detail_rows(response) or page_index >= total_pages:
            break
        page_index += 1

    return responses


def sync_price_report(
    *,
    module_name: str,
    settings: Settings | None = None,
    client: Any | None = None,
    timestamp: str | None = None,
    begin_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    runtime_settings = settings or load_settings()
    runtime_client = client or ChanjetClient(runtime_settings)
    run_timestamp = timestamp or now_timestamp()
    report_config = VERIFIED_PRICE_REPORTS[module_name]

    responses = fetch_report_responses(
        module_name=module_name,
        report_config=report_config,
        settings=runtime_settings,
        client=runtime_client,
        timestamp=run_timestamp,
        begin_date=begin_date or _default_begin_date(),
        end_date=end_date or _default_end_date(),
    )
    rows = map_report_rows(responses, report_config["columns"])
    get_logger(f"tplus_datahub.{module_name}").info("%s report sync finished: rows=%s", module_name, len(rows))
    return rows


def _default_begin_date() -> str:
    return os.getenv("PRICE_SYNC_BEGIN_DATE", DEFAULT_BEGIN_DATE).strip() or DEFAULT_BEGIN_DATE


def _default_end_date() -> str:
    # +1 天缓冲，避免 ECS(美西)与业务(东八区)跨天导致当天单据漏取。
    return (date.today() + timedelta(days=1)).isoformat()


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


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
