from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tplus_datahub.core.logger import get_logger
from tplus_datahub.jobs.db_sync_requests import (
    fetch_last_scheduled_full_at,
    fetch_next_full_request,
    fetch_platform_schedule,
    finish_full_request,
    finish_scheduler_shadow,
    fetch_next_bom_request,
    fetch_sync_config,
    finish_bom_request,
    record_scheduler_shadow,
    seed_platform_schedule,
)
from tplus_datahub.jobs.job_sync_all import run as sync_all_run
from tplus_datahub.jobs.job_sync_bom import main as sync_bom_main
from tplus_datahub.jobs.job_sync_bom import run as sync_bom_run
from tplus_datahub.jobs.sync_scheduler import (
    ScheduleDecision,
    decide as candidate_decide,
    normalize_mode,
    shadow_payload,
    target_moved_earlier,
)
from tplus_datahub.jobs.sync_state import record_tplus_sync_run_if_configured


BEIJING = timezone(timedelta(hours=8))  # 锚点时刻按北京时间解释；容器内是 UTC，中国无夏令时
DISABLED_RECHECK_MAX_SECONDS = 600


@dataclass(frozen=True)
class _CandidateRead:
    state: str
    decision: ScheduleDecision | None = None
    config: dict | None = None


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


def _legacy_control_decision(
    current: datetime, last_full: datetime | None, enabled: bool, interval_seconds: int, anchor_time: str
) -> ScheduleDecision:
    """把 T+ 既有控制流投影成可比较 decision；无锚点仍是立即 full。"""
    due = next_scheduled_full_due(current, last_full, interval_seconds, anchor_time)
    if not enabled:
        return ScheduleDecision(current + timedelta(seconds=interval_seconds), False, interval_seconds)
    if not anchor_time:
        return ScheduleDecision(due, True, 0)
    if due <= current:
        return ScheduleDecision(due, True, 0)
    return ScheduleDecision(due, False, _seconds_until_next_due(current, last_full, interval_seconds, anchor_time))


def _legacy_sleep_decision(
    current: datetime, last_full: datetime | None, enabled: bool, interval_seconds: int, anchor_time: str
) -> ScheduleDecision:
    """保留 T+ full 后固定整周期睡眠和锚点 sleep 的现有语义。"""
    wait_seconds = (
        _seconds_until_next_due(current, last_full, interval_seconds, anchor_time)
        if enabled and anchor_time
        else interval_seconds
    )
    return ScheduleDecision(current + timedelta(seconds=wait_seconds), False, wait_seconds)


def _read_candidate_decision(
    current: datetime,
    last_full: datetime | None,
    reader: Callable[[], dict | None],
) -> _CandidateRead:
    try:
        config = reader()
    except Exception:
        return _CandidateRead("error")
    if not config:
        return _CandidateRead("missing")
    try:
        enabled = bool(config.get("enabled", True))
        interval_seconds = max(int(config.get("interval_seconds") or 86400), 60)
        anchor_time = _normalize_anchor_time(config.get("anchor_time"))
        decision = candidate_decide(current, last_full, enabled, interval_seconds, anchor_time)
        if not enabled:
            wait_seconds = min(interval_seconds, DISABLED_RECHECK_MAX_SECONDS)
            decision = ScheduleDecision(current + timedelta(seconds=wait_seconds), False, wait_seconds)
        return _CandidateRead(
            "valid",
            decision,
            {
                "enabled": enabled,
                "interval_seconds": interval_seconds,
                "anchor_time": anchor_time,
            },
        )
    except Exception:
        return _CandidateRead("error")


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
        outcome = _call_full_sync(sync_once, trigger="manual")
    except Exception as exc:
        logger.exception("DB T+ full sync failed with unexpected exception: id=%s", request_id)
        finish_db_full_request(
            request_id,
            "failed",
            1,
            {
                "error": str(exc),
                "source": "manual_full",
                "platform_run_id": getattr(exc, "platform_run_id", None),
            },
        )
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
            "platform_run_id": getattr(outcome, "platform_run_id", None),
        }
    else:
        exit_code = int(outcome or 0)
        detail = {"source": "manual_full"}
    status = "success" if exit_code == 0 else "failed"
    logger.info("DB T+ full sync finished: id=%s status=%s exit_code=%s", request_id, status, exit_code)
    finish_db_full_request(request_id, status, exit_code, detail)
    return exit_code


def _call_full_sync(sync_once: Callable[[], Any], *, trigger: str) -> Any:
    if sync_once is sync_all_run:
        return sync_once(trigger=trigger)
    return sync_once()


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
    on_sleep_elapsed: Callable[[float], None] | None = None,
    after_poll: Callable[[], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int | None:
    poll_seconds = min(_read_positive_int("TPLUS_SYNC_POLL_SECONDS", 30), interval_seconds)
    remaining = interval_seconds
    last_manual_exit_code: int | None = None
    while remaining > 0:
        step = min(poll_seconds, remaining)
        sleep_started: float | None = None
        if on_sleep_elapsed is not None:
            try:
                sleep_started = monotonic()
            except Exception:
                pass
        elapsed = 0.0
        try:
            sleep(step)
        finally:
            if on_sleep_elapsed is not None and sleep_started is not None:
                try:
                    elapsed = max(monotonic() - sleep_started, 0.0)
                except Exception:
                    pass
            if on_sleep_elapsed is not None:
                try:
                    on_sleep_elapsed(elapsed)
                except Exception:
                    pass
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
        should_break = should_wake is not None and should_wake()
        if after_poll is not None:
            try:
                after_poll()
            except Exception:
                pass
        if should_break:
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
    monotonic: Callable[[], float] = time.monotonic,
    scheduler_mode_reader: Callable[[], str | None] | None = None,
    platform_schedule_reader: Callable[[], dict | None] | None = None,
    platform_schedule_seeder: Callable[[dict], None] | None = None,
    shadow_recorder: Callable[[dict], list[int]] | None = None,
    shadow_finisher: Callable[[list[int], int, bool], None] | None = None,
    max_runs: int | None = None,
) -> int:
    logger = get_logger("tplus_datahub.worker_loop", "output/logs/worker_loop.log")
    run_count = 0
    last_exit_code = 0
    read_scheduler_mode = scheduler_mode_reader or (lambda: os.getenv("SYNC_SCHEDULER_MODE"))
    read_platform_schedule = platform_schedule_reader or fetch_platform_schedule
    seed_platform_schedule_if_empty = platform_schedule_seeder or seed_platform_schedule
    record_shadow = shadow_recorder or record_scheduler_shadow
    finish_shadow = shadow_finisher or finish_scheduler_shadow
    # 从 DB 取上次定时全量的时刻，容器重建后锚点相位不丢——否则每次部署都会
    # 在白天补跑一次全量。读不到（无 DB/首次运行）就按"立即跑"处理，保持旧行为。
    try:
        last_full = read_last_full()
    except Exception:
        last_full = None

    def _run_scheduled_full(current: datetime, anchor_time: str) -> None:
        nonlocal last_full, last_exit_code
        last_full = current
        logger.info("T+ sync run started: run=%s anchor=%s", run_count, anchor_time or "-")
        platform_run_id = None
        try:
            outcome = _call_full_sync(sync_once, trigger="schedule")
        except Exception as exc:
            logger.exception("T+ sync run failed with unexpected exception: run=%s", run_count)
            platform_run_id = getattr(exc, "platform_run_id", None)
            outcome = 1
        if hasattr(outcome, "exit_code"):
            last_exit_code = int(outcome.exit_code or 0)
            export_files = list(getattr(outcome, "export_files", []) or [])
            diff_summary = getattr(outcome, "diff_summary", None)
            full_snapshot_id = getattr(outcome, "full_snapshot_id", None)
            failed_modules = list(getattr(outcome, "failed_modules", []) or [])
            failure_details = list(getattr(outcome, "failure_details", []) or [])
            platform_run_id = getattr(outcome, "platform_run_id", None)
        else:
            last_exit_code = int(outcome or 0)
            export_files = []
            diff_summary = None
            full_snapshot_id = None
            failed_modules = []
            failure_details = []
        status = "success" if last_exit_code == 0 else "failed"
        if status == "success":
            logger.info("T+ sync run finished: run=%s status=success", run_count)
        else:
            logger.error("T+ sync run finished: run=%s status=failed exit_code=%s", run_count, last_exit_code)
        try:
            record_sync_run(
                module="all",
                mode="scheduled_full",
                status=status,
                row_count=0,
                exit_code=last_exit_code,
                platform_run_id=platform_run_id,
                detail_json={
                    "run": run_count,
                    "export_files": export_files,
                    "diff_summary": diff_summary,
                    "full_snapshot_id": full_snapshot_id,
                    "failed_modules": failed_modules,
                },
                error_json={"modules": failure_details} if failure_details else {},
            )
        except Exception:
            logger.exception("Failed to record T+ sync run status: run=%s", run_count)

    def _sleep_with_legacy_wake(
        *,
        sleep_decision: ScheduleDecision,
        observe_candidate: Callable[[], None] | None = None,
        on_sleep_elapsed: Callable[[float], None] | None = None,
    ) -> int | None:
        planned_due = sleep_decision.due

        def _schedule_target_moved_earlier(_last_full=last_full, _planned=planned_due) -> bool:
            """保留 T+ 原有热读锚点的 wake 判据，legacy 仍以此为唯一实际控制。"""
            live_enabled, live_interval, live_anchor = _resolve_sync_config(read_sync_config)
            if not live_enabled or not live_anchor:
                return False
            current_now = now()
            live_target = current_now + timedelta(
                seconds=_seconds_until_next_due(current_now, _last_full, live_interval, live_anchor)
            )
            return live_target < _planned - timedelta(seconds=30)

        return _sleep_with_request_polling(
            interval_seconds=sleep_decision.wait_seconds,
            should_wake=_schedule_target_moved_earlier,
            on_sleep_elapsed=on_sleep_elapsed,
            after_poll=observe_candidate,
            monotonic=monotonic,
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

    while True:
        run_count += 1
        enabled, interval_seconds, anchor_time = _resolve_sync_config(read_sync_config)
        legacy_config = {
            "enabled": enabled,
            "interval_seconds": interval_seconds,
            "anchor_time": anchor_time,
        }
        current = now()
        try:
            mode = normalize_mode(read_scheduler_mode())
        except Exception:
            mode = "legacy"
        legacy_control = _legacy_control_decision(current, last_full, enabled, interval_seconds, anchor_time)

        candidate_read = _CandidateRead("missing")
        candidate = None
        if mode != "legacy":
            candidate_read = _read_candidate_decision(current, last_full, read_platform_schedule)
            candidate = candidate_read.decision
            if candidate_read.state == "missing":
                try:
                    seed_platform_schedule_if_empty(legacy_config)
                except Exception:
                    pass

        if mode == "active":
            active_decision = candidate or legacy_control
            if active_decision.run_full:
                _run_scheduled_full(current, anchor_time)
                after_full = now()
                refreshed = _read_candidate_decision(after_full, last_full, read_platform_schedule)
                active_decision = refreshed.decision or _legacy_sleep_decision(
                    after_full, last_full, enabled, interval_seconds, anchor_time
                )
            if max_runs is not None and run_count >= max_runs:
                return last_exit_code

            planned_candidate_due = active_decision.due

            def _active_target_moved_earlier() -> bool:
                nonlocal planned_candidate_due
                observed = now()
                refreshed = _read_candidate_decision(observed, last_full, read_platform_schedule)
                if refreshed.decision is None:
                    # 每 slice 读不到 candidate 就立刻重规划；T+ 无 anchor 的 legacy control
                    # 在该时刻是立即 full，绝不能用「full 后 sleep」投影继续等待。
                    fallback = _legacy_control_decision(observed, last_full, enabled, interval_seconds, anchor_time)
                    planned_candidate_due = fallback.due
                    return True
                fresh_decision = refreshed.decision
                moved = target_moved_earlier(planned_candidate_due, fresh_decision.due)
                planned_candidate_due = fresh_decision.due
                # manual BOM/DB/full 可在本 slice 推进 wall clock；即使 due 没有改早，
                # 当前 candidate 已 due 也必须让外层按 current 立即执行 scheduled full。
                return fresh_decision.run_full or moved

            manual_exit_code = _sleep_with_request_polling(
                interval_seconds=active_decision.wait_seconds,
                should_wake=_active_target_moved_earlier,
                monotonic=monotonic,
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
            continue

        # legacy/shadow 共用既有 control；shadow 只记录 candidate，不能改 run、wait 或热唤醒。
        if legacy_control.run_full:
            _run_scheduled_full(current, anchor_time)
        elif enabled:
            logger.info("T+ scheduled sync not due yet, skipping: run=%s next=%s anchor=%s", run_count, legacy_control.due.isoformat(), anchor_time or "-")
        else:
            logger.info("T+ scheduled sync disabled, skipping full sync: run=%s", run_count)

        shadow_run_ids: list[int] = []
        planned_candidate_due: datetime | None = None
        candidate_would_wake = False
        observed_sleep_seconds = 0.0
        if mode == "shadow":
            sampled_at = now() if legacy_control.run_full else current
            observed_candidate = (
                _read_candidate_decision(sampled_at, last_full, read_platform_schedule)
                if legacy_control.run_full
                else candidate_read
            )
            if observed_candidate.decision is not None:
                candidate_decision = observed_candidate.decision
                planned_candidate_due = candidate_decision.due
                try:
                    shadow_run_ids = record_shadow(
                        shadow_payload(sampled_at=sampled_at, legacy=legacy_control, candidate=candidate_decision)
                    )
                except Exception:
                    shadow_run_ids = []

        if max_runs is not None and run_count >= max_runs:
            if shadow_run_ids:
                try:
                    finish_shadow(shadow_run_ids, 0, False)
                except Exception:
                    pass
            return last_exit_code

        sleep_started_at = now()
        actual_legacy_sleep = _legacy_sleep_decision(
            sleep_started_at, last_full, enabled, interval_seconds, anchor_time
        )
        logger.info("T+ sync worker sleeping: seconds=%s", actual_legacy_sleep.wait_seconds)

        def _add_observed_sleep(seconds: float) -> None:
            nonlocal observed_sleep_seconds
            observed_sleep_seconds += seconds

        def _observe_shadow_candidate() -> None:
            nonlocal planned_candidate_due, candidate_would_wake
            if planned_candidate_due is None:
                return
            refreshed = _read_candidate_decision(now(), last_full, read_platform_schedule)
            if refreshed.decision is None:
                return
            refreshed_decision = refreshed.decision
            if target_moved_earlier(planned_candidate_due, refreshed_decision.due):
                candidate_would_wake = True
            planned_candidate_due = refreshed_decision.due

        try:
            manual_exit_code = _sleep_with_legacy_wake(
                sleep_decision=actual_legacy_sleep,
                observe_candidate=_observe_shadow_candidate if shadow_run_ids else None,
                on_sleep_elapsed=_add_observed_sleep if shadow_run_ids else None,
            )
            if manual_exit_code is not None:
                last_exit_code = manual_exit_code
        finally:
            if shadow_run_ids:
                try:
                    finish_shadow(shadow_run_ids, int(observed_sleep_seconds), candidate_would_wake)
                except Exception:
                    pass


def main() -> int:
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
