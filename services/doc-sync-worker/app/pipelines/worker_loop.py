from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from app.pipelines.backfill_smartsheet_images import run_backfill_images
from app.pipelines.group_message_listener import resolve_groupbot_profile, run_group_listener
from app.pipelines.sync_feishu_full import run_sync_feishu_full
from app.pipelines.sync_wecom_full import run_pending_sync_requests, run_sync_wecom_full
from app.pipelines.wecom_structure_backup import (
    run_enqueue_daily_structure_backup_jobs,
    run_pending_structure_backup_jobs,
)


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
) -> int:
    """常驻循环：启动先全量一轮，之后每 poll 间隔消费手动同步请求，按 interval 周期重跑全量。"""
    interval_seconds = _read_positive_int("DOC_SYNC_INTERVAL_SECONDS", 86400)
    poll_seconds = min(_read_positive_int("DOC_SYNC_POLL_SECONDS", 30), interval_seconds)

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
        return code

    def _default_consume_requests() -> int:
        code = run_pending_sync_requests(limit=10)
        backup_code = run_pending_structure_backup_jobs(limit=10)
        return code or backup_code

    run_full = full_sync or _default_full_sync
    run_pending = consume_requests or _default_consume_requests

    _maybe_start_group_listener()

    cycles = 0
    while True:
        print(f"[文档同步循环] 开始全量同步（interval={interval_seconds}s poll={poll_seconds}s）")
        try:
            run_full()
        except Exception as exc:  # noqa: BLE001
            print(f"[文档同步循环] 全量同步异常：{exc}")
        remaining = interval_seconds
        while remaining > 0:
            step = min(poll_seconds, remaining)
            sleep(step)
            remaining -= step
            try:
                run_pending()
            except Exception as exc:  # noqa: BLE001
                print(f"[文档同步循环] 消费手动请求异常：{exc}")
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return 0
