"""微信恢复决策实时唤醒通道（PostgreSQL LISTEN/NOTIFY + SSE）与原生卡片授权辅助。"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import anyio
import psycopg

logger = logging.getLogger("aliecs.notify.decision_realtime")

WECHAT_DIGEST_SOURCE = "wechat-digest"
DECISION_WAKE_CHANNEL = "notify_decision_wake"
WECHAT_RECOVERY_KEY_RE = re.compile(r"^(?:wechat|telegram)-login:(?!test:)[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$")
WECHAT_RECOVERY_OPTIONS = frozenset({"regen", "skip"})

WAKE_SSE_FRAME = "event: wake\ndata: {}\n\n"
HEARTBEAT_SSE_FRAME = ": heartbeat\n\n"

DEFAULT_HEARTBEAT_SECONDS = 20.0
DEFAULT_RECONNECT_INITIAL_SECONDS = 0.5
DEFAULT_RECONNECT_MAX_SECONDS = 30.0


def is_wechat_recovery_decision_key(decision_key: str) -> bool:
    """校验是否符合 wechat-login:<掉线ID>:<尝试号> 约定。"""
    key = (decision_key or "").strip()
    return bool(key and len(key) <= 128 and WECHAT_RECOVERY_KEY_RE.fullmatch(key))


def should_emit_decision_wake(source_key: str, decision_key: str, option_key: str) -> bool:
    """仅对 wechat-digest 当前协议下的恢复决策及 regen|skip 触发实时唤醒。"""
    return (
        source_key == WECHAT_DIGEST_SOURCE
        and is_wechat_recovery_decision_key(decision_key)
        and option_key in WECHAT_RECOVERY_OPTIONS
    )


def notify_decision_wake(conn: Any, source_key: str, decision_key: str, option_key: str) -> bool:
    """在同一事务中通过 PostgreSQL NOTIFY 唤醒监听器；载荷仅传来源名，不含账号或验证码。"""
    if not should_emit_decision_wake(source_key, decision_key, option_key):
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT pg_notify(%s, %s)", (DECISION_WAKE_CHANNEL, source_key))
    return True


def _parse_allowlist(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items or any(item == "*" for item in items):
        return ()
    return tuple(items)


def feishu_recovery_gateway_token() -> str:
    return os.getenv("NOTIFY_FEISHU_RECOVERY_GATEWAY_TOKEN", "").strip()


def feishu_recovery_allowed_chats() -> tuple[str, ...]:
    return _parse_allowlist(os.getenv("NOTIFY_FEISHU_RECOVERY_CHAT_ID"))


def feishu_recovery_allowed_operators() -> tuple[str, ...]:
    return _parse_allowlist(os.getenv("NOTIFY_FEISHU_RECOVERY_OPERATORS"))


def matches_allowlist(value: str, allowed: tuple[str, ...]) -> bool:
    if not value or not allowed:
        return False
    matched = False
    for item in allowed:
        matched = hmac.compare_digest(item, value) or matched
    return matched


def _positive_float_env(name: str, default: float, *, minimum: float = 0.01) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < minimum:
        return default
    return value


def heartbeat_seconds() -> float:
    return _positive_float_env("NOTIFY_DECISION_STREAM_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS)


def reconnect_bounds() -> tuple[float, float]:
    initial = _positive_float_env(
        "NOTIFY_DECISION_STREAM_RECONNECT_INITIAL_SECONDS",
        DEFAULT_RECONNECT_INITIAL_SECONDS,
        minimum=0.005,
    )
    maximum = _positive_float_env(
        "NOTIFY_DECISION_STREAM_RECONNECT_MAX_SECONDS",
        DEFAULT_RECONNECT_MAX_SECONDS,
        minimum=initial,
    )
    return initial, max(initial, maximum)


async def open_listen_connection(database_url: str) -> Any:
    """打开用于 PostgreSQL LISTEN 的独立异步连接（必须 autocommit=True）。"""
    if not database_url:
        raise RuntimeError("DATABASE_URL is empty")
    return await psycopg.AsyncConnection.connect(
        database_url,
        autocommit=True,
        connect_timeout=3,
    )


async def close_listen_connection(conn: Any) -> None:
    if conn is None:
        return
    try:
        res = conn.close()
        if inspect.isawaitable(res):
            with anyio.CancelScope(shield=True):
                await asyncio.wait_for(res, timeout=2.0)
    except Exception:
        pass


async def start_decision_listen(database_url: str) -> Any:
    """建立真实 PostgreSQL LISTEN 会话；连接无效或 LISTEN 失败时立即关闭并抛错。"""
    conn = await open_listen_connection(database_url)
    try:
        if conn is None or getattr(conn, "closed", False):
            raise ConnectionError("invalid listen connection")
        res = conn.execute(f"LISTEN {DECISION_WAKE_CHANNEL}")
        if inspect.isawaitable(res):
            await res
        if getattr(conn, "closed", False):
            raise ConnectionError("listen connection closed during LISTEN")
        return conn
    except Exception:
        await close_listen_connection(conn)
        raise


class StreamHandle:
    """跟踪单个 SSE 流的事件循环、取消句柄与底层 LISTEN 连接。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, conn: Any | None = None) -> None:
        self.loop = loop
        self.stop_event = asyncio.Event()
        self.conn = conn
        self.task: asyncio.Task[Any] | None = None

    def _stop_in_loop(self) -> None:
        self.stop_event.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()

    def request_stop(self) -> None:
        if self.loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._stop_in_loop()
        else:
            try:
                self.loop.call_soon_threadsafe(self._stop_in_loop)
            except RuntimeError:
                pass


_active_streams: set[StreamHandle] = set()


def register_stream(conn: Any) -> StreamHandle:
    loop = asyncio.get_running_loop()
    handle = StreamHandle(loop=loop, conn=conn)
    _active_streams.add(handle)
    return handle


def active_stream_count() -> int:
    return len(_active_streams)


async def shutdown_decision_streams() -> None:
    """在应用关闭时主动停止全部活动 SSE 流并释放其 LISTEN 连接。"""
    handles = list(_active_streams)
    for handle in handles:
        handle.request_stop()
    for handle in handles:
        task = handle.task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except BaseException:
                pass
        conn = handle.conn
        if conn is not None:
            handle.conn = None
            await close_listen_connection(conn)
        _active_streams.discard(handle)


def _notify_matches_source(notify: Any, source_key: str) -> bool:
    channel = getattr(notify, "channel", None)
    payload = getattr(notify, "payload", None)
    if channel is None and isinstance(notify, tuple) and len(notify) >= 2:
        channel, payload = notify[0], notify[1]
    return channel == DECISION_WAKE_CHANNEL and payload == source_key


async def decision_wake_stream(
    *,
    source_key: str,
    database_url: str,
    initial_conn: Any,
    handle: StreamHandle,
    heartbeat_interval: float,
    reconnect_initial: float,
    reconnect_max: float,
) -> AsyncIterator[str]:
    """基于真实 PG LISTEN 的 SSE 生成器。

    - 调用方须在进入本流前完成首次 connect + LISTEN，确保首条 wake 就绪时监听器已在岗，
      客户端据此回查 by-key 即可覆盖早于 LISTEN 的提交；
    - 空闲仅发 SSE 注释心跳，不轮询业务表；
    - 忽略非本来源信号；
    - 底层 PG 连接断开后执行指数退避重连，重连成功即发一次 wake 补齐断线窗口。
    """
    conn = initial_conn
    handle.task = asyncio.current_task()
    handle.conn = conn
    try:
        yield WAKE_SSE_FRAME
        last_output_at = time.monotonic()
        reconnect_delay = reconnect_initial

        while not handle.stop_event.is_set():
            if conn is None:
                try:
                    conn = await start_decision_listen(database_url)
                    handle.conn = conn
                    reconnect_delay = reconnect_initial
                    yield WAKE_SSE_FRAME
                    last_output_at = time.monotonic()
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "decision stream listen reconnect failed source=%s error=%s",
                        source_key,
                        type(exc).__name__,
                    )
                    wait_deadline = time.monotonic() + reconnect_delay
                    reconnect_delay = min(reconnect_max, reconnect_delay * 2.0)
                    while not handle.stop_event.is_set():
                        now = time.monotonic()
                        if now >= wait_deadline:
                            break
                        next_heartbeat = last_output_at + heartbeat_interval
                        if now >= next_heartbeat:
                            yield HEARTBEAT_SSE_FRAME
                            last_output_at = time.monotonic()
                            continue
                        step = min(wait_deadline - now, next_heartbeat - now)
                        try:
                            await asyncio.wait_for(
                                handle.stop_event.wait(), timeout=max(0.005, step)
                            )
                            break
                        except asyncio.TimeoutError:
                            pass
                    continue

            try:
                if getattr(conn, "closed", False):
                    raise ConnectionError("listen connection closed")
                now = time.monotonic()
                remaining = (last_output_at + heartbeat_interval) - now
                if remaining <= 0:
                    yield HEARTBEAT_SSE_FRAME
                    last_output_at = time.monotonic()
                    continue

                emitted_wake = False
                async for notify in conn.notifies(timeout=remaining):
                    if handle.stop_event.is_set():
                        break
                    if not _notify_matches_source(notify, source_key):
                        if time.monotonic() - last_output_at >= heartbeat_interval:
                            yield HEARTBEAT_SSE_FRAME
                            last_output_at = time.monotonic()
                        continue
                    yield WAKE_SSE_FRAME
                    last_output_at = time.monotonic()
                    emitted_wake = True
                    break

                if handle.stop_event.is_set():
                    break

                if not emitted_wake and (time.monotonic() - last_output_at >= heartbeat_interval):
                    yield HEARTBEAT_SSE_FRAME
                    last_output_at = time.monotonic()

                if getattr(conn, "closed", False):
                    raise ConnectionError("listen connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "decision stream listen dropped source=%s error=%s",
                    source_key,
                    type(exc).__name__,
                )
                await close_listen_connection(conn)
                conn = None
                handle.conn = None
    finally:
        handle.stop_event.set()
        _active_streams.discard(handle)
        if conn is not None:
            await close_listen_connection(conn)
            handle.conn = None
