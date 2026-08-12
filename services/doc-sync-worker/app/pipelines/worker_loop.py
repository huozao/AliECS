from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.pipelines.backfill_smartsheet_images import run_backfill_images
from app.pipelines.group_message_listener import resolve_groupbot_profile, run_group_listener
from app.pipelines.rnd_record_writer import run_write_rnd_records
from app.pipelines.sync_feishu_full import run_sync_feishu_full
from app.pipelines.sync_schedule import (
    next_full_sync_due,
    pull_config_from_bitable,
    read_last_full_run,
    read_schedule_config,
)
from app.pipelines.sync_wecom_full import run_pending_sync_requests, run_sync_wecom_full
from app.pipelines.tplus_parent_match import run_backfill_if_bom_synced, run_tplus_parent_match
from app.pipelines.wecom_structure_backup import (
    run_enqueue_daily_structure_backup_jobs,
    run_pending_structure_backup_jobs,
)


CONFIG_PULL_MIN_SECONDS = 120
DISABLED_RECHECK_MAX_SECONDS = 600


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


def run_worker_loop(
    *,
    full_sync: Callable[[], int] | None = None,
    consume_requests: Callable[[], int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    schedule_reader: Callable[[], dict[str, Any]] | None = None,
    config_puller: Callable[[], str] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    last_full_reader: Callable[[], datetime | None] | None = None,
) -> int:
    """常驻循环：调度配置（开关/周期/起点时间）每大轮热读 DB，到点跑全量；
    等待期每 poll 间隔消费手动同步请求并定期从飞书「配置表」拉配置。
    重启时先看上次全量时间，没到点不重跑（修"重启即全量"）。"""
    poll_seconds = _read_positive_int("DOC_SYNC_POLL_SECONDS", 30)
    is_default_pipeline = full_sync is None and consume_requests is None
    read_config = schedule_reader or read_schedule_config
    now = now_fn or (lambda: datetime.now(timezone.utc))
    read_last_full = last_full_reader or read_last_full_run
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
            run_enqueue_daily_structure_backup_jobs()
            run_pending_structure_backup_jobs(limit=1000)
        except Exception as exc:  # noqa: BLE001 - 备份失败由持久化任务重试，不拖垮源同步。
            print(f"[文档同步循环] 企微结构备份异常：{exc}")
        # 放在全量同步之后：核对要读刚同步下来的 T+ BOM 与表格最新内容。
        try:
            run_tplus_parent_match(trigger="schedule")
        except Exception as exc:  # noqa: BLE001 - 核对失败不拖垮源同步周期
            print(f"[文档同步循环] T+ 父件核对异常：{exc}")
        return code

    def _default_consume_requests() -> int:
        nonlocal bom_watermark
        code = run_pending_sync_requests(limit=10)
        backup_code = run_pending_structure_backup_jobs(limit=10)
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
        return code or backup_code

    run_full = full_sync or _default_full_sync
    run_pending = consume_requests or _default_consume_requests

    _maybe_start_group_listener()

    try:
        last_full = read_last_full()
    except Exception:  # noqa: BLE001
        last_full = None
    last_pull_monotonic: float | None = None
    cycles = 0
    while True:
        config = read_config()
        interval_seconds = max(int(config.get("interval_seconds") or 86400), 60)
        anchor_time = str(config.get("anchor_time") or "")
        enabled = bool(config.get("enabled", True))
        current = now()
        if not enabled:
            print(f"[文档同步循环] 定时同步已关闭，仅消费手动请求（poll={poll_seconds}s）。")
            remaining = float(min(interval_seconds, DISABLED_RECHECK_MAX_SECONDS))
        else:
            due = next_full_sync_due(current, last_full, interval_seconds, anchor_time)
            if due <= current:
                print(
                    f"[文档同步循环] 开始全量同步（interval={interval_seconds}s poll={poll_seconds}s "
                    f"anchor={anchor_time or '-'}）"
                )
                last_full = current
                try:
                    run_full()
                except Exception as exc:  # noqa: BLE001
                    print(f"[文档同步循环] 全量同步异常：{exc}")
                due = next_full_sync_due(current, last_full, interval_seconds, anchor_time)
            else:
                print(f"[文档同步循环] 上次全量未到期（下次 {due.isoformat()}），跳过启动全量。")
            remaining = max((due - current).total_seconds(), 0.0)
        while remaining > 0:
            step = min(poll_seconds, remaining)
            sleep(step)
            remaining -= step
            try:
                run_pending()
            except Exception as exc:  # noqa: BLE001
                print(f"[文档同步循环] 消费手动请求异常：{exc}")
            mono = time.monotonic()
            if last_pull_monotonic is None or mono - last_pull_monotonic >= CONFIG_PULL_MIN_SECONDS:
                last_pull_monotonic = mono
                try:
                    result = pull_config()
                    if result and not result.startswith("noop"):
                        print(f"[文档同步循环] 配置表拉取：{result}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[文档同步循环] 配置表拉取异常：{exc}")
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return 0
