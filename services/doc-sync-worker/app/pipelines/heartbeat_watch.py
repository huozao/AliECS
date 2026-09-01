"""aliecs 流量心跳的缺失告警。

aliecs（阿里云 ECS，美西）按使用流量计费，月闸门 200GB；2026-08-15 越线后公网被
限速到约 3.4 KB/s，08-16 起停机一个月。恢复运行后由设备侧的 aliecs-traffic-guard
每天发一条流量日报进 notify_outbox（source_key = ALIECS_TRAFFIC_HEARTBEAT_SOURCE）。

**这个模块守的是「日报没来」这件事，而不是流量本身。**
理由：aliecs 侧的告警要经公网 POST 到本机的 /api/v1/internal/notify/send，而
「出网被限速」正是它最该报警的场景——那时这条 POST 自己也出不去。设备侧再怎么做
重试和落盘补发，都补不上「设备已经打不出去了」这一种。只有在**接收端**看
「该来的没来」，才覆盖得到。

⚠️ 告警本身必须用**别的** source_key 写回 outbox（这里是 doc-sync）。若用被监视的
那个 source_key，这条告警行自己就会把心跳「续上」，下一轮检查发现「最近有行」于是
不再告警——自己消掉自己的触发条件，且三处观测面全都正常。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from app import notify_client

# 告警自身的来源。必须与被监视的 source 不同，理由见模块 docstring。
ALERT_SOURCE = "doc-sync"
ALERT_EVENT = "aliecs.traffic.heartbeat_missing"

DEFAULT_SOURCE = "aliecs-traffic"
DEFAULT_CHECK_INTERVAL_SECONDS = 3600.0

_last_checked_monotonic: float | None = None


def reset_throttle() -> None:
    """测试用：清掉节流状态。"""
    global _last_checked_monotonic
    _last_checked_monotonic = None


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def max_age_hours() -> float:
    """0 或负数 = 关闭这条看护。

    默认关闭是有意的：采集器还没装、或装了还没成功发出第一条时打开，会天天报
    「心跳缺失」而那只是「还没上线」。把开关这个动作留给「已经收到过第一条」之后，
    等于用一个人工确认换掉一整类假告警。
    """
    return _env_float("ALIECS_TRAFFIC_HEARTBEAT_MAX_AGE_HOURS", 0.0)


def _latest_heartbeat_at(conn: Any, source_key: str) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(created_at) FROM notify_outbox WHERE source_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    if not row:
        return None
    value = row[0]
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def check_heartbeat(*, now: datetime | None = None, force: bool = False) -> bool:
    """查一次 aliecs 流量心跳，缺失则写一条 error 进 outbox。返回是否真的查了库。

    返回值刻意是「有没有查库」而不是「有没有告警」：调用方（_poll_once）每轮都调它，
    真正查库受节流控制，测试要能把这两件事分开断言。

    整个函数不抛异常——看护绝不能弄挂同步主循环。
    """
    global _last_checked_monotonic
    threshold_hours = max_age_hours()
    if threshold_hours <= 0:
        return False

    if not force:
        interval = _env_float(
            "ALIECS_TRAFFIC_HEARTBEAT_CHECK_INTERVAL_SECONDS", DEFAULT_CHECK_INTERVAL_SECONDS
        )
        try:
            stamp = monotonic()
        except Exception:  # noqa: BLE001 - 时钟读不到就当没到点，下轮再说
            return False
        if _last_checked_monotonic is not None and stamp - _last_checked_monotonic < interval:
            return False
        _last_checked_monotonic = stamp

    source_key = (os.getenv("ALIECS_TRAFFIC_HEARTBEAT_SOURCE") or DEFAULT_SOURCE).strip()
    moment = now or datetime.now(timezone.utc)

    try:
        conn = notify_client.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"[心跳看护] 连接数据库失败：{type(exc).__name__}")
        return True
    try:
        latest = _latest_heartbeat_at(conn, source_key)
        age_hours = None if latest is None else (moment - latest).total_seconds() / 3600.0
        if age_hours is not None and age_hours <= threshold_hours:
            return True

        if latest is None:
            detail = "从未收到过任何心跳"
            last_seen = "（无记录）"
        else:
            detail = f"已经 {age_hours:.1f} 小时没有新的心跳"
            last_seen = latest.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        payload = notify_client.build_payload(
            source=ALERT_SOURCE,
            event=ALERT_EVENT,
            level="error",
            title="aliecs 流量心跳缺失",
            summary=f"{source_key} {detail}（阈值 {threshold_hours:.0f} 小时）。",
            fields=[
                ("来源", source_key),
                ("最后一条", last_seen),
                ("阈值", f"{threshold_hours:.0f} 小时"),
            ],
            text_segments=[
                "可能是采集器停了、aliecs 关机了，或者**出网已经被限速导致它发不出来**——"
                "最后这种正是流量告警最该起作用却起不了作用的情况，所以才有这条反向看护。",
                "先确认 aliecs 是否在线，再看 aliecs-traffic-guard.timer 的 NextElapseUSec 和 "
                "/var/lib/aliecs-traffic/pending.jsonl 里有没有堆积的待补发。",
            ],
            occurred_at=moment,
        )
        # 按 UTC 日期做幂等：断了多久都只在每天第一次检查时留一条，不刷屏。
        dedup_key = f"aliecs-traffic-heartbeat-missing:{moment.strftime('%Y-%m-%d')}"
        notify_client.enqueue(payload, dedup_key=dedup_key, conn=conn)
        return True
    except Exception as exc:  # noqa: BLE001 - 看护绝不能影响同步主循环
        print(f"[心跳看护] 检查异常：{type(exc).__name__}")
        return True
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
