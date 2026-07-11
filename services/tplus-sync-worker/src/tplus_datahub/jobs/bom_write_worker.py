"""独立 T+ BOM 写入 worker；默认关闭，避免读同步进程获得写权限。"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.core.logger import get_logger
from tplus_datahub.jobs.db_bom_submissions import add_event, claim_next_submission, finish_submission


BOM_CREATE_ENDPOINT = "/tplus/api/v2/bom/Create"
BOM_QUERY_ENDPOINT = "/tplus/api/v2/bom/Query"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _result_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("result", "Result", "value", "Value"):
        value = response.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return None


def _verified_bom(response: Any, parent_code: str, version: str, result_id: str) -> dict[str, Any] | None:
    rows = response if isinstance(response, list) else []
    if isinstance(response, dict):
        for key in ("result", "Result", "data", "Data", "value", "Value"):
            if isinstance(response.get(key), list):
                rows = response[key]
                break
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("Code") or (row.get("Inventory") or {}).get("Code") or "").strip()
        row_version = str(row.get("Version") or "").strip()
        row_id = str(row.get("ID") or row.get("Id") or row.get("id") or "").strip()
        if code == parent_code and row_version == version and (not result_id or row_id == result_id):
            return row
    return None


def process_submission(submission: dict[str, Any], client: ChanjetClient | None = None) -> str:
    client = client or ChanjetClient()
    submission_id = int(submission["id"])
    payload = submission.get("request_json") or {}
    dto = payload.get("dto") or {}
    parent_code = str((dto.get("Inventory") or {}).get("Code") or "").strip()
    version = str(dto.get("Version") or "").strip()
    query_payload = {"dto": {"Code": parent_code, "Version": version}}
    try:
        preflight_response = client.post(BOM_QUERY_ENDPOINT, query_payload)
    except Exception as exc:
        finish_submission(
            submission_id, status="failed",
            error={"type": type(exc).__name__, "message": f"写入前查询失败：{exc}"},
        )
        return "failed"
    existing = _verified_bom(preflight_response, parent_code, version, "")
    if existing is not None:
        finish_submission(
            submission_id, status="failed", verification={"existing": existing},
            error={"message": "T+ 已存在相同父件编码和版本号的 BOM"},
        )
        return "failed"

    try:
        add_event(submission_id, "create_requested", {"parent_code": parent_code, "version": version})
        create_response = client.post(BOM_CREATE_ENDPOINT, payload)
        result_id = _result_id(create_response)
        if not result_id:
            finish_submission(
                submission_id, status="failed", response=create_response,
                error={"message": "T+ 创建接口未返回 BOM ID"},
            )
            return "failed"
        add_event(submission_id, "create_accepted", {"result_bom_id": result_id})
        query_response = client.post(BOM_QUERY_ENDPOINT, query_payload)
        verified = _verified_bom(query_response, parent_code, version, result_id)
        if verified is None:
            finish_submission(
                submission_id, status="needs_review", response=create_response, verification=query_response,
                error={"message": "T+ 已返回 BOM ID，但写后查询未确认到同一记录"}, result_bom_id=result_id,
            )
            return "needs_review"
        finish_submission(
            submission_id, status="success", response=create_response,
            verification={"ID": result_id, "Code": parent_code, "Version": version}, result_bom_id=result_id,
        )
        return "success"
    except Exception as exc:
        # 网络超时可能发生在 T+ 已落库之后，禁止自动重试以免重复创建。
        finish_submission(
            submission_id, status="needs_review",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        return "needs_review"


def run_forever(
    *,
    claim: Callable[[], dict[str, Any] | None] = claim_next_submission,
    process: Callable[[dict[str, Any]], str] = process_submission,
    sleep: Callable[[float], None] = time.sleep,
    max_polls: int | None = None,
) -> int:
    poll_seconds = max(1, int(os.getenv("TPLUS_BOM_WRITE_POLL_SECONDS", "5")))
    logger = get_logger("tplus_datahub.bom_write_worker", "output/logs/bom_write_worker.log")
    polls = 0
    if not _truthy(os.getenv("TPLUS_BOM_WRITE_ENABLED", "false")):
        logger.warning("T+ BOM write worker is disabled")
        while max_polls is None or polls < max_polls:
            polls += 1
            sleep(60)
        return 0
    while True:
        polls += 1
        submission = claim()
        if submission is None:
            if max_polls is not None and polls >= max_polls:
                return 0
            sleep(poll_seconds)
            continue
        logger.info("T+ BOM write started: submission_id=%s", submission["id"])
        status = process(submission)
        logger.info("T+ BOM write finished: submission_id=%s status=%s", submission["id"], status)
        if max_polls is not None and polls >= max_polls:
            return 0


def main() -> int:
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
