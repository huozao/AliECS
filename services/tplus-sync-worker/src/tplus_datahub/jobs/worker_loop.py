from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tplus_datahub.core.logger import get_logger
from tplus_datahub.jobs.db_sync_requests import (
    fetch_last_scheduled_full_at,
    fetch_next_full_request,
    finish_full_request,
    fetch_next_bom_request,
    fetch_sync_config,
    finish_bom_request,
)
from tplus_datahub.jobs.job_sync_all import run as sync_all_run
from tplus_datahub.jobs.job_sync_bom import main as sync_bom_main
from tplus_datahub.jobs.job_sync_bom import run as sync_bom_run
from tplus_datahub.jobs.sync_state import record_tplus_sync_run_if_configured


BEIJING = timezone(timedelta(hours=8))  # 锚点时刻按北京时间解释；容器内是 UTC，中国无夏令时


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


def _resolve_sync_config(read_sync_config: Callable[[], dict | None]) -> tuple[bool, int, str]:
    """解析定时同步配置 → (enabled, interval_seconds, anchor_time)。
    任何异常/缺失/非法值都回退到 env 默认（enabled 视为 true），保证不阻断 worker。"""
    try:
        cfg = read_sync_config()
    except Exception:
        cfg = None
    env_interval = _read_positive_int("TPLUS_SYNC_INTERVAL_SECONDS", 86400)
    if not cfg:
        return True, env_interval, ""
    enabled = bool(cfg.get("enabled", True))
    try:
        interval = int(cfg.get("interval_seconds"))
    except (TypeError, ValueError):
        interval = 0
    if interval <= 0:
        interval = env_interval
    return enabled, interval, _normalize_anchor_time(cfg.get("anchor_time"))


def _normalize_anchor_time(value: Any) -> str:
    """只接受 HH:MM（北京时间）。非法值一律当作未设锚点，不阻断 worker。"""
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.split(":")
    if len(parts) != 2:
        return ""
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return ""
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return ""


def next_scheduled_full_due(
    now: datetime, last_full: datetime | None, interval_seconds: int, anchor_time: str
) -> datetime:
    """下一次定时全量应跑的时刻（aware-UTC）。从未跑过=立即；无锚点=上次+周期；
    锚点 HH:MM（北京时间）=相位对齐到 {锚点 + k*周期} 序列中大于上次的最小值。

    与 doc-sync 的 next_full_sync_due 同一套语义，便于两个 worker 行为一致。
    """
    interval = max(int(interval_seconds), 60)
    if last_full is None:
        return now
    if last_full.tzinfo is None:
        last_full = last_full.replace(tzinfo=timezone.utc)
    if not anchor_time:
        return last_full + timedelta(seconds=interval)
    hour, minute = (int(part) for part in anchor_time.split(":"))
    anchor_local = last_full.astimezone(BEIJING).replace(hour=hour, minute=minute, second=0, microsecond=0)
    anchor = anchor_local.astimezone(timezone.utc)
    # 把 anchor 移到 last_full 之前，再逐周期前进到第一个大于 last_full 的点。
    if anchor > last_full:
        steps = int((anchor - last_full).total_seconds() // interval) + 1
        anchor -= timedelta(seconds=steps * interval)
    due = anchor + timedelta(seconds=interval)
    while due <= last_full:
        due += timedelta(seconds=interval)
    return due


def _seconds_until_next_due(
    current: datetime, last_full: datetime | None, interval_seconds: int, anchor_time: str
) -> int:
    """距下一次定时全量的等待秒数。全量本身跑过了锚点时刻才结束时顺延一个周期，
    避免算出 1 秒等待反复空转。"""
    interval = max(int(interval_seconds), 60)
    due = next_scheduled_full_due(current, last_full, interval, anchor_time)
    while due <= current:
        due += timedelta(seconds=interval)
    return max(int((due - current).total_seconds()), 1)


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


def _run_pending_db_full_request(
    *,
    fetch_db_full_request: Callable[..., dict | None],
    finish_db_full_request: Callable[[int, str, int, dict], None],
    sync_once: Callable[[], Any],
    logger,
) -> int | None:
    """消费页面「立即全量同步」排的队。跑的是定时全量同一个 sync_once。

    记账走 finish_db_full_request（mode='manual_full'），不碰 run_forever 的 last_full——
    手动补一次不该顶掉当天的定时轮次。
    """
    if not _truthy(os.getenv("TPLUS_DB_SYNC_REQUESTS_ENABLED", "true")):
        return None
    request = fetch_db_full_request(limit=5)
    if request is None:
        return None
    request_id = int(request["id"])
    logger.info("DB T+ full sync request detected: id=%s", request_id)
    try:
        outcome = sync_once()
    except Exception as exc:
        logger.exception("DB T+ full sync failed with unexpected exception: id=%s", request_id)
        finish_db_full_request(request_id, "failed", 1, {"error": str(exc), "source": "manual_full"})
        return 1

    if hasattr(outcome, "exit_code"):
        exit_code = int(outcome.exit_code or 0)
        detail = {
            "source": "manual_full",
            "export_files": list(getattr(outcome, "export_files", []) or []),
            "diff_summary": getattr(outcome, "diff_summary", None),
            "full_snapshot_id": getattr(outcome, "full_snapshot_id", None),
            "failed_modules": list(getattr(outcome, "failed_modules", []) or []),
            "failure_details": list(getattr(outcome, "failure_details", []) or []),
        }
    else:
        exit_code = int(outcome or 0)
        detail = {"source": "manual_full"}
    status = "success" if exit_code == 0 else "failed"
    logger.info("DB T+ full sync finished: id=%s status=%s exit_code=%s", request_id, status, exit_code)
    finish_db_full_request(request_id, status, exit_code, detail)
    return exit_code


def _sleep_with_request_polling(
    *,
    interval_seconds: int,
    sleep: Callable[[int], None],
    sync_once: Callable[[], Any],
    sync_bom_once: Callable[[], int | None],
    sync_bom_request_once: Callable[[dict], int | None],
    fetch_db_bom_request: Callable[..., dict | None],
    finish_db_bom_request: Callable[[int, str, int, dict], None],
    fetch_db_full_request: Callable[..., dict | None],
    finish_db_full_request: Callable[[int, str, int, dict], None],
    logger,
    should_wake: Callable[[], bool] | None = None,
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
        full_exit_code = _run_pending_db_full_request(
            fetch_db_full_request=fetch_db_full_request,
            finish_db_full_request=finish_db_full_request,
            sync_once=sync_once,
            logger=logger,
        )
        if full_exit_code is not None:
            last_manual_exit_code = full_exit_code
        # 睡眠时长是进睡前一次性算好的，中途改调度不会自动生效——不在这里热读，
        # 改一次执行时刻最长要等一整个周期（2026-08-11 实测：改 01:00 后当晚整轮没跑）。
        if should_wake is not None and should_wake():
            logger.info("T+ schedule target moved earlier, replanning sleep: remaining=%s", remaining)
            break
    return last_manual_exit_code


def _default_fetch_db_bom_request(limit: int = 5) -> dict | None:
    return fetch_next_bom_request(limit=limit)


def _default_finish_db_bom_request(request_id: int, status: str, exit_code: int, detail: dict) -> None:
    finish_bom_request(request_id, status, exit_code, detail)


def _default_fetch_db_full_request(limit: int = 5) -> dict | None:
    return fetch_next_full_request(limit=limit)


def _default_finish_db_full_request(request_id: int, status: str, exit_code: int, detail: dict) -> None:
    finish_full_request(request_id, status, exit_code, detail)


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
    fetch_db_full_request: Callable[..., dict | None] = _default_fetch_db_full_request,
    finish_db_full_request: Callable[[int, str, int, dict], None] = _default_finish_db_full_request,
    record_sync_run: Callable[..., int | None] = record_tplus_sync_run_if_configured,
    read_sync_config: Callable[[], dict | None] = _default_read_sync_config,
    read_last_full: Callable[[], Any | None] = fetch_last_scheduled_full_at,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[int], None] = time.sleep,
    max_runs: int | None = None,
) -> int:
    logger = get_logger("tplus_datahub.worker_loop", "output/logs/worker_loop.log")
    run_count = 0
    last_exit_code = 0
    # 从 DB 取上次定时全量的时刻，容器重建后锚点相位不丢——否则每次部署都会
    # 在白天补跑一次全量。读不到（无 DB/首次运行）就按"立即跑"处理，保持旧行为。
    try:
        last_full = read_last_full()
    except Exception:
        last_full = None

    while True:
        run_count += 1
        # 每轮热读配置：关掉只跳过定时全量同步（手动/订阅照常）；间隔和锚点改了下一轮即生效。
        enabled, interval_seconds, anchor_time = _resolve_sync_config(read_sync_config)
        current = now()
        due = next_scheduled_full_due(current, last_full, interval_seconds, anchor_time)
        # 未到期就只睡到到期时刻（手动/订阅同步在睡眠中照常轮询消费）。
        # 只有设了锚点才做到期判断——没设锚点时保持"跑完睡一个周期"的原行为不变。
        not_due = enabled and bool(anchor_time) and due > current
        run_full = enabled and not not_due
        if run_full:
            last_full = current
            logger.info("T+ sync run started: run=%s anchor=%s", run_count, anchor_time or "-")
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
                failed_modules = list(getattr(outcome, "failed_modules", []) or [])
                failure_details = list(getattr(outcome, "failure_details", []) or [])
            else:
                last_exit_code = int(outcome or 0)
                export_files = []
                diff_summary = None
                full_snapshot_id = None
                failed_modules = []
                failure_details = []

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
                    # failed_modules 是 backend 侧告警的唯一依据（模块独立容错后，
                    # 整轮可能 status=success 却有个别模块没数据），别在写入侧省掉。
                    detail_json={"run": run_count, "export_files": export_files,
                                 "diff_summary": diff_summary, "full_snapshot_id": full_snapshot_id,
                                 "failed_modules": failed_modules},
                    # 失败详情必须落库：只看 detail_json.failed_modules 只知道「bom 挂了」，
                    # 不知道为什么挂（2026-08-09 的 read timeout=30 就是这么被埋了四天）。
                    error_json={"modules": failure_details} if failure_details else {},
                )
            except Exception:
                logger.exception("Failed to record T+ sync run status: run=%s", run_count)
        elif not_due:
            logger.info(
                "T+ scheduled sync not due yet, skipping: run=%s next=%s anchor=%s",
                run_count, due.isoformat(), anchor_time or "-",
            )
        else:
            logger.info("T+ scheduled sync disabled, skipping full sync: run=%s", run_count)

        if max_runs is not None and run_count >= max_runs:
            return last_exit_code

        # 睡到下一个锚点，而不是固定睡一个周期——固定睡会把睡眠期内的锚点整轮跳过，
        # 醒来时刻又变成新的相位，锚点永远收敛不了（2026-08-05 生产实测）。
        sleep_started_at = now()
        wait_seconds = (
            _seconds_until_next_due(sleep_started_at, last_full, interval_seconds, anchor_time)
            if enabled and anchor_time
            else interval_seconds
        )
        planned_due = sleep_started_at + timedelta(seconds=wait_seconds)
        logger.info("T+ sync worker sleeping: seconds=%s", wait_seconds)

        def _schedule_target_moved_earlier(_last_full=last_full, _planned=planned_due) -> bool:
            """睡眠中每个轮询片热读一次调度配置：目标时刻被改早了就退出睡眠，让主循环
            按新配置重新规划。不热读的话，改一次执行时刻最长要等一整个周期才生效
            （2026-08-11 实测：改成 01:00 后当晚整轮没跑，连失败记录都没有）。

            判据必须是「目标时刻提前了」而不是「到期时刻已过」——全量自己跑过了锚点
            才结束时，到期时刻同样是过去式，按后者会当场空转重跑。
            关掉定时不算改早：不跑就是不跑，没必要把 worker 叫起来。"""
            live_enabled, live_interval, live_anchor = _resolve_sync_config(read_sync_config)
            if not live_enabled or not live_anchor:
                return False
            current_now = now()
            live_target = current_now + timedelta(
                seconds=_seconds_until_next_due(current_now, _last_full, live_interval, live_anchor)
            )
            # 30 秒容差：等待秒数取整会有个位数抖动，而配置最小粒度是分钟，不会误判。
            return live_target < _planned - timedelta(seconds=30)

        manual_exit_code = _sleep_with_request_polling(
            interval_seconds=wait_seconds,
            should_wake=_schedule_target_moved_earlier,
            sleep=sleep,
            sync_once=sync_once,
            sync_bom_once=sync_bom_once,
            sync_bom_request_once=sync_bom_request_once,
            fetch_db_bom_request=fetch_db_bom_request,
            finish_db_bom_request=finish_db_bom_request,
            fetch_db_full_request=fetch_db_full_request,
            finish_db_full_request=finish_db_full_request,
            logger=logger,
        )
        if manual_exit_code is not None:
            last_exit_code = manual_exit_code


def main() -> int:
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
