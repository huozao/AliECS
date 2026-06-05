from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from tplus_datahub.core.logger import get_logger
from tplus_datahub.jobs.job_sync_bom import main as sync_bom_main
from tplus_datahub.jobs.job_sync_all import main as sync_all_main


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


def _sleep_with_manual_bom_polling(
    *,
    interval_seconds: int,
    sleep: Callable[[int], None],
    sync_bom_once: Callable[[], int | None],
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
    return last_manual_exit_code


def run_forever(
    *,
    sync_once: Callable[[], int | None] = sync_all_main,
    sync_bom_once: Callable[[], int | None] = sync_bom_main,
    sleep: Callable[[int], None] = time.sleep,
    max_runs: int | None = None,
) -> int:
    logger = get_logger("tplus_datahub.worker_loop", "output/logs/worker_loop.log")
    interval_seconds = _read_positive_int("TPLUS_SYNC_INTERVAL_SECONDS", 3600)
    run_count = 0
    last_exit_code = 0

    while True:
        run_count += 1
        logger.info("T+ sync run started: run=%s", run_count)
        try:
            last_exit_code = int(sync_once() or 0)
        except Exception:
            logger.exception("T+ sync run failed with unexpected exception: run=%s", run_count)
            last_exit_code = 1

        if last_exit_code == 0:
            logger.info("T+ sync run finished: run=%s status=success", run_count)
        else:
            logger.error("T+ sync run finished: run=%s status=failed exit_code=%s", run_count, last_exit_code)

        if max_runs is not None and run_count >= max_runs:
            return last_exit_code

        logger.info("T+ sync worker sleeping: seconds=%s", interval_seconds)
        manual_exit_code = _sleep_with_manual_bom_polling(
            interval_seconds=interval_seconds,
            sleep=sleep,
            sync_bom_once=sync_bom_once,
            logger=logger,
        )
        if manual_exit_code is not None:
            last_exit_code = manual_exit_code


def main() -> int:
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
