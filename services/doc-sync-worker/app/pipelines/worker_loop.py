from __future__ import annotations

import os
import time
from collections.abc import Callable

from app.pipelines.sync_feishu_full import run_sync_feishu_full
from app.pipelines.sync_wecom_full import run_pending_sync_requests, run_sync_wecom_full


def _read_positive_int(name: str, default: int) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


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
            run_sync_feishu_full()
        except Exception as exc:  # noqa: BLE001 - 飞书未配置源时不拖垮 wecom 周期
            print(f"[文档同步循环] 飞书全量跳过：{exc}")
        return code

    run_full = full_sync or _default_full_sync
    run_pending = consume_requests or (lambda: run_pending_sync_requests(limit=10))

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
