from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from app import notify_client
from app.pipelines.backfill_smartsheet_images import run_backfill_images
from app.pipelines.document_locator import reconcile_document_locators
from app.pipelines.document_locator_mirror import (
    run_pending_document_locator_mirror_jobs,
    run_sheet_inventory_mirror,
)
from app.pipelines.group_message_listener import resolve_groupbot_profile, run_group_listener
from app.pipelines.rnd_record_writer import run_write_rnd_records
from app.pipelines.sync_feishu_full import run_sync_feishu_full
from app.pipelines import sync_alert_notifier
from app.pipelines.sync_schedule import (
    pull_config_from_bitable,
    read_last_full_run,
    read_platform_schedule,
    read_schedule_config,
    seed_platform_schedule,
)
from app.pipelines.sync_scheduler import ScheduleDecision, decide, normalize_mode, shadow_payload, target_moved_earlier
from app.pipelines.sync_wecom_full import run_pending_sync_requests, run_sync_wecom_full
from app.pipelines.tplus_parent_match import run_backfill_if_bom_synced, run_tplus_parent_match
from app.storage.postgres import open_store
from app.storage.job_catalog import reconcile_document_jobs_fail_open


CONFIG_PULL_MIN_SECONDS = 120
DISABLED_RECHECK_MAX_SECONDS = 600
# 运行记录保留期。每作业保底条数不能省：整簿跳过留痕后写入量涨到约 90 行/天，
# 纯按时间删会把低频作业删成「无记录」——正是页面上最容易被误读成"这作业坏了"的状态。
RUN_RETENTION_DAYS = 90
RUN_RETENTION_MIN_PER_JOB = 5


def prune_sync_job_runs(
    retain_days: int = RUN_RETENTION_DAYS, min_runs_per_job: int = RUN_RETENTION_MIN_PER_JOB
) -> int:
    store = open_store()
    try:
        if not hasattr(store, "prune_sync_job_runs"):
            return 0
        return int(store.prune_sync_job_runs(retain_days, min_runs_per_job) or 0)
    finally:
        store.close()


def _read_positive_int(name: str, default: int) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _maybe_start_group_listener() -> None:
    """配了群机器人长连接凭据时，起一个常驻守护线程接收群@消息（与同步周期互不阻塞）。"""
    profile = resolve_groupbot_profile("")
    if not profile:
        print("[文档同步循环] 未配置群机器人长连接凭据，跳过群监听。")
        return

    def _runner() -> None:
        try:
            run_group_listener(profiles_arg=profile)
        except Exception as exc:  # noqa: BLE001 - 监听线程异常不拖垮主循环
            print(f"[文档同步循环] 群监听线程退出：{exc}")

    thread = threading.Thread(target=_runner, name="group-listener", daemon=True)
    thread.start()
    print(f"[文档同步循环] 群监听守护线程已启动（{profile}）。")


def _record_scheduler_shadow(payload: dict[str, Any]) -> list[int]:
    """影子观测写入独立 fail-open，绝不影响既有同步循环。"""
    try:
        store = open_store()
        try:
            return store.record_scheduler_shadow(payload)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - 影子记录失败不得改变生产行为
        return []


def _finish_scheduler_shadow(run_ids: list[int], observed_sleep_seconds: int, candidate_would_wake: bool) -> None:
    """只按 recorder 返回的精确 run id 收尾影子观测。"""
    try:
        store = open_store()
        try:
            store.finish_scheduler_shadow(run_ids, observed_sleep_seconds, candidate_would_wake)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - 影子收尾失败不得改变生产行为
        pass


def _reconcile_platform_catalog() -> None:
    """Own one startup store lifecycle and keep catalog failures fail-open."""
    try:
        store = open_store()
        try:
            reconcile_document_jobs_fail_open(store)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - catalog bootstrap must not block legacy sync
        pass


def _reconcile_locator_catalog() -> None:
    """Refresh the private locator archive at startup without blocking legacy sync."""
    try:
        store = open_store()
        try:
            reconcile_document_locators(store, trigger="worker-startup")
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - locator observability must remain fail-open
        pass


def _schedule_decision(current: datetime, last_full: datetime | None, config: dict[str, Any]) -> ScheduleDecision:
    enabled = bool(config.get("enabled", True))
    interval_seconds = max(int(config.get("interval_seconds") or 86400), 60)
    decision = decide(
        current,
        last_full,
        enabled=enabled,
        interval_seconds=interval_seconds,
        anchor_time=str(config.get("anchor_time") or ""),
    )
    if enabled:
        return decision
    wait_seconds = min(interval_seconds, DISABLED_RECHECK_MAX_SECONDS)
    return ScheduleDecision(current + timedelta(seconds=wait_seconds), False, wait_seconds)


def _read_candidate_decision(
    current: datetime,
    last_full: datetime | None,
    reader: Callable[[], dict[str, Any] | None],
) -> tuple[ScheduleDecision, bool] | None:
    try:
        config = reader()
        if not config:
            return None
        return _schedule_decision(current, last_full, config), bool(config.get("enabled", True))
    except Exception:  # noqa: BLE001 - platform 调度读取异常一律回退 legacy
        return None


def run_worker_loop(
    *,
    full_sync: Callable[[], int] | None = None,
    consume_requests: Callable[[], int] | None = None,
    notifier_once: Callable[[], dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    schedule_reader: Callable[[], dict[str, Any]] | None = None,
    config_puller: Callable[[], str] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    last_full_reader: Callable[[], datetime | None] | None = None,
    scheduler_mode_reader: Callable[[], str | None] | None = None,
    platform_schedule_reader: Callable[[], dict[str, Any] | None] | None = None,
    platform_schedule_seeder: Callable[[dict[str, Any]], None] | None = None,
    shadow_recorder: Callable[[dict[str, Any]], list[int]] | None = None,
    shadow_finisher: Callable[[list[int], int, bool], None] | None = None,
) -> int:
    """常驻循环：调度配置（开关/周期/起点时间）每大轮热读 DB，到点跑全量；
    等待期每 poll 间隔消费手动同步请求并定期从飞书「配置表」拉配置。
    重启时先看上次全量时间，没到点不重跑（修"重启即全量"）。"""
    poll_seconds = _read_positive_int("DOC_SYNC_POLL_SECONDS", 30)
    is_default_pipeline = full_sync is None and consume_requests is None
    read_config = schedule_reader or read_schedule_config
    now = now_fn or (lambda: datetime.now(timezone.utc))
    read_last_full = last_full_reader or read_last_full_run
    read_scheduler_mode = scheduler_mode_reader or (lambda: os.getenv("SYNC_SCHEDULER_MODE"))
    read_platform_schedule_config = platform_schedule_reader or read_platform_schedule
    seed_platform_schedule_if_empty = platform_schedule_seeder or seed_platform_schedule
    record_shadow = shadow_recorder or _record_scheduler_shadow
    finish_shadow = shadow_finisher or _finish_scheduler_shadow
    # 默认 puller 只在生产装配（未注入 full/consume）时启用，测试注入路径不碰网络/DB。
    if config_puller is not None:
        pull_config = config_puller
    elif is_default_pipeline:
        pull_config = pull_config_from_bitable
    else:
        pull_config = lambda: "noop"  # noqa: E731

    # BOM 同步水位存进程内存即可：补建幂等，重启后首轮只记水位不跑。
    bom_watermark: datetime | None = None

    def _default_full_sync() -> int:
        code = run_sync_wecom_full()
        try:
            result = run_backfill_images()
            print(
                "[文档同步循环] 图片回填完成："
                f"targets={result.target_count} scanned={result.scanned_count} "
                f"updated={result.updated_count} errors={result.error_count}"
            )
        except Exception as exc:  # noqa: BLE001 - 图片回填不拖垮文档同步周期
            print(f"[文档同步循环] 图片回填跳过：{exc}")
        try:
            run_sync_feishu_full()
        except Exception as exc:  # noqa: BLE001 - 飞书未配置源时不拖垮 wecom 周期
            print(f"[文档同步循环] 飞书全量跳过：{exc}")
        try:
            run_pending_document_locator_mirror_jobs(limit=1000)
        except Exception as exc:  # noqa: BLE001 - durable locator jobs retry independently.
            print(f"[文档同步循环] 文档定位档案镜像异常：{type(exc).__name__}")
        # 放在定位档案之后：表级来源刚随全量同步增删完，此时刷出来的身份才是最新的。
        # 新子表被发现不会产生文档级 locator 事件，所以这一步必须独立于 mirror job。
        try:
            run_sheet_inventory_mirror()
        except Exception as exc:  # noqa: BLE001 - 清单镜像不得拖垮同步周期。
            print(f"[文档同步循环] 同步表格清单镜像异常：{type(exc).__name__}")
        # 放在全量同步之后：核对要读刚同步下来的 T+ BOM 与表格最新内容。
        try:
            run_tplus_parent_match(trigger="schedule")
        except Exception as exc:  # noqa: BLE001 - 核对失败不拖垮源同步周期
            print(f"[文档同步循环] T+ 父件核对异常：{exc}")
        # 挂在每日全量之后：这是循环里唯一天然的「一天一次」入口，不必再引入 pg_cron。
        try:
            deleted = prune_sync_job_runs()
            print(
                f"[文档同步循环] 运行记录清理：删除 {deleted} 条"
                f"（保留 {RUN_RETENTION_DAYS} 天，每作业保底 {RUN_RETENTION_MIN_PER_JOB} 条）。"
            )
        except Exception as exc:  # noqa: BLE001 - 清理失败不拖垮同步周期
            print(f"[文档同步循环] 运行记录清理异常：{type(exc).__name__}")
        return code

    def _default_consume_requests() -> int:
        nonlocal bom_watermark
        code = run_pending_sync_requests(limit=10)
        mirror_code = run_pending_document_locator_mirror_jobs(limit=10)
        try:
            written = run_write_rnd_records()
            if written:
                print(f"[文档同步循环] 研发过程记录写入 {written} 条。")
        except Exception as exc:  # noqa: BLE001 - 写表失败不拖垮轮询
            print(f"[文档同步循环] 研发过程记录写入异常：{exc}")
        # T+ BOM 同步完成后立刻补建，不必等次日兜底轮。
        try:
            bom_watermark, ran = run_backfill_if_bom_synced(bom_watermark)
            if ran:
                print("[文档同步循环] 检测到 T+ BOM 新同步，已跑一次父件核对与补建。")
        except Exception as exc:  # noqa: BLE001 - 核对失败不拖垮轮询
            print(f"[文档同步循环] T+ 事件触发核对异常：{exc}")
        return code or mirror_code

    run_full = full_sync or _default_full_sync
    run_pending = consume_requests or _default_consume_requests
    if notifier_once is not None:
        run_notifier = notifier_once
    elif is_default_pipeline:
        run_notifier = sync_alert_notifier.run_notifier_once
    else:
        run_notifier = lambda: {}  # noqa: E731

    def _notify_fail_open() -> None:
        try:
            run_notifier()
        except Exception as exc:  # noqa: BLE001 - 告警失败不拖垮同步主循环
            print(f"[文档同步循环] 告警检查异常：{type(exc).__name__}")

    if is_default_pipeline:
        _reconcile_platform_catalog()
        _reconcile_locator_catalog()
    _maybe_start_group_listener()

    try:
        last_full = read_last_full()
    except Exception:  # noqa: BLE001
        last_full = None
    last_pull_monotonic: float | None = None

    def _poll_once(
        step: float,
        *,
        observe_clock: bool,
        on_sleep_elapsed: Callable[[float], None] | None = None,
    ) -> datetime | None:
        """跑一个既有 poll；睡眠测量仅包住 sleep，且测量异常不能覆盖业务异常。"""
        nonlocal last_pull_monotonic
        sleep_started: float | None = None
        try:
            sleep_started = monotonic()
        except Exception:  # noqa: BLE001 - 观测时钟故障不影响 worker
            pass
        elapsed = 0.0
        try:
            sleep(step)
        finally:
            if sleep_started is not None:
                try:
                    elapsed = max(monotonic() - sleep_started, 0.0)
                except Exception:  # noqa: BLE001 - 不让观测覆盖 sleep 的原异常
                    pass
            if on_sleep_elapsed is not None:
                try:
                    on_sleep_elapsed(elapsed)
                except Exception:  # noqa: BLE001 - 影子观测回调不得影响主循环
                    pass
        try:
            run_pending()
        except Exception as exc:  # noqa: BLE001
            print(f"[文档同步循环] 消费手动请求异常：{exc}")
        _notify_fail_open()
        try:
            mono = monotonic()
        except Exception:  # noqa: BLE001 - 配置拉取节流时钟故障，保留本轮主流程
            mono = None
        if mono is not None and (last_pull_monotonic is None or mono - last_pull_monotonic >= CONFIG_PULL_MIN_SECONDS):
            last_pull_monotonic = mono
            try:
                result = pull_config()
                if result and not result.startswith("noop"):
                    print(f"[文档同步循环] 配置表拉取：{result}")
            except Exception as exc:  # noqa: BLE001
                print(f"[文档同步循环] 配置表拉取异常：{exc}")
        return now() if observe_clock else None

    def _run_scheduled_full(current: datetime, message: str) -> None:
        nonlocal last_full
        print(message)
        last_full = current
        if not terminal_poll_covers_preflight:
            _notify_fail_open()
        try:
            run_full()
        except Exception as exc:  # noqa: BLE001
            print(f"[文档同步循环] 全量同步异常：{exc}")

    # 仅去重紧邻的 terminal poll / next preflight；每个 poll 仍独立告警。
    terminal_poll_covered_next_preflight = False
    cycles = 0
    while True:
        # 请中枢带走上一轮写进 notify_outbox 的通知。worker 只写库不投递，
        # 所以这一脚油门决定了 worker 侧告警的最大延迟（约一个轮询周期）。
        # 调不通不影响任何东西——行已落库，下一轮再带。
        try:
            notify_client.request_flush()
        except Exception as exc:  # noqa: BLE001 - 通知冲刷绝不能影响同步主循环
            print(f"[文档同步循环] 通知冲刷异常：{type(exc).__name__}")
        config = read_config()
        interval_seconds = max(int(config.get("interval_seconds") or 86400), 60)
        anchor_time = str(config.get("anchor_time") or "")
        enabled = bool(config.get("enabled", True))
        current = now()
        try:
            mode = normalize_mode(read_scheduler_mode())
        except Exception:  # noqa: BLE001 - 模式读取失败一律保留 legacy
            mode = "legacy"
        terminal_poll_covers_preflight = terminal_poll_covered_next_preflight
        terminal_poll_covered_next_preflight = False
        legacy_decision = _schedule_decision(current, last_full, config)
        candidate: tuple[ScheduleDecision, bool] | None = None
        if mode != "legacy":
            candidate = _read_candidate_decision(current, last_full, read_platform_schedule_config)
            if candidate is None:
                candidate_config = {
                    "enabled": enabled,
                    "interval_seconds": interval_seconds,
                    "anchor_time": anchor_time,
                }
                try:
                    seed_platform_schedule_if_empty(candidate_config)
                except Exception:  # noqa: BLE001 - 注入 seeder 也必须 fail-open
                    pass
                candidate = _read_candidate_decision(current, last_full, read_platform_schedule_config)

        if mode == "active":
            active_decision = candidate[0] if candidate is not None else legacy_decision
            active_disabled_recheck = candidate is not None and not candidate[1]
            if active_decision.run_full:
                prefix = "platform" if candidate is not None else "legacy fallback"
                _run_scheduled_full(
                    current,
                    f"[文档同步循环] 开始全量同步（{prefix} due={active_decision.due.isoformat()} "
                    f"poll={poll_seconds}s）",
                )
                after_full = now()
                legacy_decision = _schedule_decision(after_full, last_full, config)
                candidate = _read_candidate_decision(after_full, last_full, read_platform_schedule_config)
                active_decision = candidate[0] if candidate is not None else legacy_decision
                active_disabled_recheck = candidate is not None and not candidate[1]
            else:
                print(
                    f"[文档同步循环] platform 全量未到期（下次 {active_decision.due.isoformat()}），跳过启动全量。"
                )
            planned_candidate_due = active_decision.due
            remaining = float(active_decision.wait_seconds)
            expected_terminal_boundary = active_decision.due
            while remaining > 0:
                step = min(poll_seconds, remaining)
                remaining -= step
                observed = _poll_once(step, observe_clock=True)
                assert observed is not None
                if remaining <= 0:
                    terminal_poll_covered_next_preflight = observed >= expected_terminal_boundary
                refreshed_candidate = _read_candidate_decision(observed, last_full, read_platform_schedule_config)
                if refreshed_candidate is None:
                    active_decision = _schedule_decision(observed, last_full, config)
                    active_disabled_recheck = False
                    planned_candidate_due = active_decision.due
                    expected_terminal_boundary = active_decision.due
                    remaining = float(active_decision.wait_seconds)
                    if remaining <= 0:
                        terminal_poll_covered_next_preflight = observed >= expected_terminal_boundary
                    continue
                refreshed_decision, refreshed_enabled = refreshed_candidate
                if not refreshed_enabled and active_disabled_recheck:
                    # disabled 的 bounded recheck 是固定本轮目标，不能每个 poll 从 now 重新滚动 60s。
                    continue
                if target_moved_earlier(planned_candidate_due, refreshed_decision.due):
                    if refreshed_decision.run_full:
                        terminal_poll_covered_next_preflight = True
                    break
                active_decision = refreshed_decision
                active_disabled_recheck = not refreshed_enabled
                planned_candidate_due = refreshed_decision.due
                expected_terminal_boundary = refreshed_decision.due
                remaining = float(refreshed_decision.wait_seconds)
                if remaining <= 0:
                    terminal_poll_covered_next_preflight = observed >= expected_terminal_boundary
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return 0
            continue

        # legacy 和 shadow 共用既有实际调度。shadow 观测不得改写这份真实等待决策。
        actual_legacy_sleep_decision = legacy_decision
        shadow_observation_sampled_at = current
        if not enabled:
            print(f"[文档同步循环] 定时同步已关闭，仅消费手动请求（poll={poll_seconds}s）。")
        elif legacy_decision.run_full:
            _run_scheduled_full(
                current,
                f"[文档同步循环] 开始全量同步（interval={interval_seconds}s poll={poll_seconds}s "
                f"anchor={anchor_time or '-'}）",
            )
            actual_legacy_sleep_decision = _schedule_decision(current, last_full, config)
            if mode == "shadow":
                shadow_observation_sampled_at = now()
        else:
            print(f"[文档同步循环] 上次全量未到期（下次 {legacy_decision.due.isoformat()}），跳过启动全量。")

        shadow_run_ids: list[int] = []
        planned_candidate_due: datetime | None = None
        candidate_would_wake = False
        if mode == "shadow":
            shadow_observation_candidate = (
                _read_candidate_decision(
                    shadow_observation_sampled_at,
                    last_full,
                    read_platform_schedule_config,
                )
                if legacy_decision.run_full
                else candidate
            )
            if shadow_observation_candidate is not None:
                candidate_decision, _ = shadow_observation_candidate
                planned_candidate_due = candidate_decision.due
                try:
                    shadow_run_ids = record_shadow(
                        shadow_payload(
                            sampled_at=shadow_observation_sampled_at,
                            legacy=actual_legacy_sleep_decision,
                            candidate=candidate_decision,
                        )
                    )
                except Exception:  # noqa: BLE001 - 注入 writer 也必须 fail-open
                    shadow_run_ids = []

        remaining = float(actual_legacy_sleep_decision.wait_seconds)
        expected_terminal_boundary = actual_legacy_sleep_decision.due
        observed_sleep_seconds = 0.0

        def _add_observed_sleep(seconds: float) -> None:
            nonlocal observed_sleep_seconds
            observed_sleep_seconds += seconds

        try:
            while remaining > 0:
                step = min(poll_seconds, remaining)
                remaining -= step
                observed = _poll_once(
                    step,
                    observe_clock=remaining <= 0 or planned_candidate_due is not None,
                    on_sleep_elapsed=_add_observed_sleep if shadow_run_ids else None,
                )
                if remaining <= 0:
                    assert observed is not None
                    terminal_poll_covered_next_preflight = observed >= expected_terminal_boundary
                if planned_candidate_due is not None:
                    assert observed is not None
                    refreshed_candidate = _read_candidate_decision(observed, last_full, read_platform_schedule_config)
                    if refreshed_candidate is not None:
                        refreshed_decision, _ = refreshed_candidate
                        if target_moved_earlier(planned_candidate_due, refreshed_decision.due):
                            candidate_would_wake = True
                        planned_candidate_due = refreshed_decision.due
        finally:
            if shadow_run_ids:
                try:
                    finish_shadow(shadow_run_ids, int(observed_sleep_seconds), candidate_would_wake)
                except Exception:  # noqa: BLE001 - 注入 finisher 也必须 fail-open
                    pass
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return 0
