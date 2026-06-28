from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tplus_datahub.core.logger import get_logger
from tplus_datahub.jobs.db_sync_requests import fetch_next_bom_request, fetch_sync_config, finish_bom_request
from tplus_datahub.jobs.job_sync_all import run as sync_all_run
from tplus_datahub.jobs.job_sync_bom import main as sync_bom_main
from tplus_datahub.jobs.job_sync_bom import run as sync_bom_run
from tplus_datahub.jobs.sync_state import record_tplus_sync_run_if_configured


def _read_positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got: {raw_value}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0, got: {raw_value}")
    return value


def _request_dir() -> Path:
    return Path(os.getenv("TPLUS_BOM_SYNC_REQUEST_DIR", "/app/sync-requests"))


def _next_bom_request() -> Path | None:
    request_dir = _request_dir()
    if not request_dir.is_dir():
        return None
    requests = sorted(path for path in request_dir.glob("*.json") if path.is_file())
    return requests[0] if requests else None


def _finish_bom_request(request_path: Path, exit_code: int) -> None:
    suffix = ".done" if exit_code == 0 else ".failed"
    target = request_path.with_suffix(request_path.suffix + suffix)
    try:
        request_path.replace(target)
    except FileNotFoundError:
        return


def _run_pending_bom_request(sync_bom_once: Callable[[], int | None], logger) -> int | None:
    request_path = _next_bom_request()
    if request_path is None:
        return None
    logger.info("Manual T+ BOM sync request detected: %s", request_path.name)
    try:
        exit_code = int(sync_bom_once() or 0)
    except Exception:
        logger.exception("Manual T+ BOM sync failed with unexpected exception: %s", request_path.name)
        exit_code = 1
    _finish_bom_request(request_path, exit_code)
    if exit_code == 0:
        logger.info("Manual T+ BOM sync finished: request=%s status=success", request_path.name)
    else:
        logger.error("Manual T+ BOM sync finished: request=%s status=failed exit_code=%s", request_path.name, exit_code)
    return exit_code


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _default_read_sync_config() -> dict | None:
    return fetch_sync_config()


def _resolve_sync_config(read_sync_config: Callable[[], dict | None]) -> tuple[bool, int]:
    """解析定时同步配置 → (enabled, interval_seconds)。
    任何异常/缺失/非法值都回退到 env 默认（enabled 视为 true），保证不阻断 worker。"""
    try:
        cfg = read_sync_config()
    except Exception:
        cfg = None
    env_interval = _read_positive_int("TPLUS_SYNC_INTERVAL_SECONDS", 86400)
    if not cfg:
        return True, env_interval
    enabled = bool(cfg.get("enabled", True))
    try:
        interval = int(cfg.get("interval_seconds"))
    except (TypeError, ValueError):
        interval = 0
    if interval <= 0:
        interval = env_interval
    return enabled, interval


def _run_pending_db_bom_request(
    *,
    fetch_db_bom_request: Callable[..., dict | None],
    finish_db_bom_request: Callable[[int, str, int, dict], None],
    sync_bom_request_once: Callable[[dict], Any],
    logger,
) -> int | None:
    if not _truthy(os.getenv("TPLUS_DB_SYNC_REQUESTS_ENABLED", "true")):
        return None
    request = fetch_db_bom_request(limit=5)
    if request is None:
        return None
    request_id = int(request["id"])
    logger.info("DB T+ BOM sync request detected: id=%s mode=%s", request_id, request.get("mode"))
    try:
        result = sync_bom_request_once(request)
        exit_code = int(getattr(result, "exit_code", result) or 0)
        export_files = list(getattr(result, "export_files", []) or [])
        diff_summary = getattr(result, "diff_summary", None)
        full_snapshot_id = getattr(result, "full_snapshot_id", None)
    except Exception as exc:
        logger.exception("DB T+ BOM sync failed with unexpected exception: id=%s", request_id)
        exit_code = 1
        export_files = []
        diff_summary = None
        full_snapshot_id = None
        detail = {"error": str(exc), "mode": request.get("mode"), "target_json": request.get("target_json") or {}}
    else:
        detail = {"mode": request.get("mode"), "target_json": request.get("target_json") or {},
                  "export_files": export_files, "diff_summary": diff_summary, "full_snapshot_id": full_snapshot_id}
    status = "success" if exit_code == 0 else "failed"
    finish_db_bom_request(request_id, status, exit_code, detail)
    return exit_code


def _sleep_with_manual_bom_polling(
    *,
    interval_seconds: int,
    sleep: Callable[[int], None],
    sync_bom_once: Callable[[], int | None],
    sync_bom_request_once: Callable[[dict], int | None],
    fetch_db_bom_request: Callable[..., dict | None],
    finish_db_bom_request: Callable[[int, str, int, dict], None],
    logger,
) -> int | None:
    poll_seconds = min(_read_positive_int("TPLUS_SYNC_POLL_SECONDS", 30), interval_seconds)
    remaining = interval_seconds
    last_manual_exit_code: int | None = None
    while remaining > 0:
        step = min(poll_seconds, remaining)
        sleep(step)
        remaining -= step
        manual_exit_code = _run_pending_bom_request(sync_bom_once, logger)
        if manual_exit_code is not None:
            last_manual_exit_code = manual_exit_code
        db_exit_code = _run_pending_db_bom_request(
            fetch_db_bom_request=fetch_db_bom_request,
            finish_db_bom_request=finish_db_bom_request,
            sync_bom_request_once=sync_bom_request_once,
            logger=logger,
        )
        if db_exit_code is not None:
            last_manual_exit_code = db_exit_code
    return last_manual_exit_code


def _default_fetch_db_bom_request(limit: int = 5) -> dict | None:
    return fetch_next_bom_request(limit=limit)


def _default_finish_db_bom_request(request_id: int, status: str, exit_code: int, detail: dict) -> None:
    finish_bom_request(request_id, status, exit_code, detail)


def run_forever(
    *,
    sync_once: Callable[[], Any] = sync_all_run,
    sync_bom_once: Callable[[], int | None] = sync_bom_main,
    sync_bom_request_once: Callable[[dict], Any] = lambda request: sync_bom_run(
        target=request.get("target_json") or {},
        mode=str(request.get("mode") or "incremental"),
    ),
    fetch_db_bom_request: Callable[..., dict | None] = _default_fetch_db_bom_request,
    finish_db_bom_request: Callable[[int, str, int, dict], None] = _default_finish_db_bom_request,
    record_sync_run: Callable[..., int | None] = record_tplus_sync_run_if_configured,
    read_sync_config: Callable[[], dict | None] = _default_read_sync_config,
    sleep: Callable[[int], None] = time.sleep,
    max_runs: int | None = None,
) -> int:
    logger = get_logger("tplus_datahub.worker_loop", "output/logs/worker_loop.log")
    run_count = 0
    last_exit_code = 0

    while True:
        run_count += 1
        # 每轮热读配置：关掉只跳过定时全量同步（手动/订阅照常）；间隔改了下一轮即生效。
        enabled, interval_seconds = _resolve_sync_config(read_sync_config)
        if enabled:
            logger.info("T+ sync run started: run=%s", run_count)
            try:
                outcome = sync_once()
            except Exception:
                logger.exception("T+ sync run failed with unexpected exception: run=%s", run_count)
                outcome = 1
            if hasattr(outcome, "exit_code"):
                last_exit_code = int(outcome.exit_code or 0)
                export_files = list(getattr(outcome, "export_files", []) or [])
                diff_summary = getattr(outcome, "diff_summary", None)
                full_snapshot_id = getattr(outcome, "full_snapshot_id", None)
            else:
                last_exit_code = int(outcome or 0)
                export_files = []
                diff_summary = None
                full_snapshot_id = None

            if last_exit_code == 0:
                logger.info("T+ sync run finished: run=%s status=success", run_count)
                status = "success"
            else:
                logger.error("T+ sync run finished: run=%s status=failed exit_code=%s", run_count, last_exit_code)
                status = "failed"

            try:
                record_sync_run(
                    module="all",
                    mode="scheduled_full",
                    status=status,
                    row_count=0,
                    exit_code=last_exit_code,
                    detail_json={"run": run_count, "export_files": export_files,
                                 "diff_summary": diff_summary, "full_snapshot_id": full_snapshot_id},
                    error_json={},
                )
            except Exception:
                logger.exception("Failed to record T+ sync run status: run=%s", run_count)
        else:
            logger.info("T+ scheduled sync disabled, skipping full sync: run=%s", run_count)

        if max_runs is not None and run_count >= max_runs:
            return last_exit_code

        logger.info("T+ sync worker sleeping: seconds=%s", interval_seconds)
        manual_exit_code = _sleep_with_manual_bom_polling(
            interval_seconds=interval_seconds,
            sleep=sleep,
            sync_bom_once=sync_bom_once,
            sync_bom_request_once=sync_bom_request_once,
            fetch_db_bom_request=fetch_db_bom_request,
            finish_db_bom_request=finish_db_bom_request,
            logger=logger,
        )
        if manual_exit_code is not None:
            last_exit_code = manual_exit_code


def main() -> int:
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
