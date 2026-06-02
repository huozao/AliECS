from __future__ import annotations

import os
import time
from collections.abc import Callable

from tplus_datahub.core.logger import get_logger
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


def run_forever(
    *,
    sync_once: Callable[[], int | None] = sync_all_main,
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
        sleep(interval_seconds)


def main() -> int:
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
