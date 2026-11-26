"""微信掉线恢复实时唤醒（PG LISTEN/NOTIFY + SSE）与飞书原生卡片入口测试。

覆盖离线可测的全链路语义与可选真实 PostgreSQL 集成测试：
1. 确认页 POST 保留；source=wechat-digest + key=wechat-login:<掉线ID>:<尝试号> + option=regen|skip
   在同一事务提交后发送 PostgreSQL NOTIFY，载荷仅含来源名；回滚不触发唤醒。
2. SSE 端点 GET /v1/internal/notify/decisions/stream 复用 _require_source、先建立 LISTEN 再发送
   就绪 wake（覆盖早于 LISTEN 的提交）、空闲仅发心跳、忽略其他来源、PG 断线重连补发 wake、
   无效连接 fail-closed、客户端断开与服务关闭释放。
3. 原生卡片入口 POST /v1/internal/notify/decisions/feishu-choice 严格校验 4 个字段、独立令牌、
   群与操作者白名单（fail-closed）、当前 QR 决策校验、同选项重投/并发幂等、共享历史记录。
4. 旧普通决策语义与零 NOTIFY 行为完整保留。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import sys
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


def _ensure_backend_app() -> None:
    resident = sys.modules.get("app")
    if resident is not None:
        paths = list(getattr(resident, "__path__", []) or [])
        if not any(str(BACKEND_ROOT) in p for p in paths):
            for name in list(sys.modules):
                if name == "app" or name.startswith("app."):
                    del sys.modules[name]
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))


_ensure_backend_app()

from app.notify import decision_realtime, dispatch, store  # noqa: E402
from app.notify.models import Notification  # noqa: E402
from app.routers import notify as notify_router  # noqa: E402


class FakeNotify:
    def __init__(self, channel: str, payload: str, pid: int = 1) -> None:
        self.channel = channel
        self.payload = payload
        self.pid = pid


class FakeAsyncListenConnection:
    """模拟支持真实阻塞等待、手动注错与断开的 psycopg.AsyncConnection。"""

    def __init__(self, cluster: "TransactionalFakePostgres", conn_id: int) -> None:
        self.cluster = cluster
        self.conn_id = conn_id
        self.closed = False
        self.listening_channels: set[str] = set()
        self.executed: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._pending_error: Exception | None = None

    def push_notify(self, channel: str, payload: str) -> None:
        if self.closed or channel not in self.listening_channels:
            return
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, FakeNotify(channel, payload))

    def inject_error(self, exc: Exception) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, exc)
        else:
            self._pending_error = exc

    async def execute(self, query: str) -> None:
        if self.closed:
            raise ConnectionError("connection closed")
        self._loop = asyncio.get_running_loop()
        self.executed.append(query)
        upper = query.strip()
        if upper.upper().startswith("LISTEN "):
            channel = upper.split(None, 1)[1].strip().strip('"')
            self.listening_channels.add(channel)

    async def notifies(
        self, *, timeout: float | None = None, stop_after: int | None = None
    ):
        self._loop = asyncio.get_running_loop()
        if self._pending_error is not None:
            err = self._pending_error
            self._pending_error = None
            raise err
        deadline = None if timeout is None else (time.monotonic() + timeout)
        received = 0
        while True:
            if self.closed:
                raise ConnectionError("listen connection closed")
            wait_timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            if wait_timeout is not None and wait_timeout <= 0 and self._queue.empty():
                break
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
            received += 1
            if stop_after is not None and received >= stop_after:
                break

    async def close(self) -> None:
        self.closed = True
        self.cluster.unregister_listener(self)


class FakeSyncCursor:
    def __init__(self, conn: "FakeSyncConnection") -> None:
        self.conn = conn
        self._last_row: Any = None
        self._last_rowset: list[Any] = []

    def __enter__(self) -> "FakeSyncCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        norm = " ".join(sql.split())
        self.conn.cluster.executed_sqls.append((norm, params))

        if "SELECT token_sha256 FROM notify_sources WHERE source_key = %s AND enabled" in norm:
            source_key = params[0]
            rec = self.conn.cluster.sources.get(source_key)
            if rec and rec["enabled"]:
                self._last_row = (rec["token_sha256"],)
            else:
                self._last_row = None

        elif "SELECT id, status FROM notify_decisions WHERE source_key = %s AND decision_key = %s" in norm:
            source_key, dec_key = params[0], params[1]
            with self.conn.cluster.lock:
                found = None
                for d in self.conn.cluster.decisions.values():
                    if d["source_key"] == source_key and d["decision_key"] == dec_key:
                        found = (d["id"], d["status"])
                        break
                self._last_row = found

        elif "INSERT INTO notify_decisions" in norm:
            with self.conn.cluster.lock:
                d_id = self.conn.cluster.next_decision_id
                self.conn.cluster.next_decision_id += 1
                now = datetime.now(timezone.utc)
                self.conn.cluster.decisions[d_id] = {
                    "id": d_id,
                    "source_key": params[0],
                    "decision_key": params[1],
                    "question": params[2],
                    "options_json": params[3].obj if hasattr(params[3], "obj") else params[3],
                    "status": "pending",
                    "chosen_option": None,
                    "chosen_label": None,
                    "chosen_at": None,
                    "created_at": now,
                    "updated_at": now,
                }
                self._last_row = (d_id,)

        elif "INSERT INTO notify_decision_tokens" in norm:
            with self.conn.cluster.lock:
                self.conn.cluster.tokens.append((int(params[0]), params[1]))

        elif "SELECT token_sha256 FROM notify_decision_tokens WHERE decision_id = %s" in norm:
            d_id = int(params[0])
            with self.conn.cluster.lock:
                self._last_rowset = [(h,) for (dec, h) in self.conn.cluster.tokens if dec == d_id]

        elif "SELECT id, source_key, decision_key, question, options_json, status, chosen_option, chosen_label, chosen_at, created_at, updated_at FROM notify_decisions WHERE id = %s" in norm:
            d_id = int(params[0])
            with self.conn.cluster.lock:
                d = self.conn.cluster.decisions.get(d_id)
                self._last_row = (
                    (
                        d["id"],
                        d["source_key"],
                        d["decision_key"],
                        d["question"],
                        d["options_json"],
                        d["status"],
                        d["chosen_option"],
                        d["chosen_label"],
                        d["chosen_at"],
                        d["created_at"],
                        d["updated_at"],
                    )
                    if d
                    else None
                )

        elif "WHERE source_key = %s AND decision_key LIKE %s ORDER BY created_at DESC, id DESC LIMIT 20" in norm:
            source_key, like_pat = params[0], params[1]
            prefix = like_pat[:-1] if like_pat.endswith("%") else like_pat
            if "FOR UPDATE" in norm:
                self.conn.acquire_row_lock()
            with self.conn.cluster.lock:
                matched = [
                    d
                    for d in self.conn.cluster.decisions.values()
                    if d["source_key"] == source_key and str(d["decision_key"]).startswith(prefix)
                ]
                matched.sort(key=lambda d: (d["created_at"], d["id"]), reverse=True)
                self._last_rowset = [
                    (
                        d["id"],
                        d["source_key"],
                        d["decision_key"],
                        d["question"],
                        d["options_json"],
                        d["status"],
                        d["chosen_option"],
                        d["chosen_label"],
                        d["chosen_at"],
                        d["created_at"],
                        d["updated_at"],
                    )
                    for d in matched[:20]
                ]

        elif "INSERT INTO notify_decision_events" in norm:
            d_id = int(params[0])
            now = datetime.now(timezone.utc)
            self.conn.pending_events.append(
                {
                    "decision_id": d_id,
                    "option_key": params[1],
                    "option_label": params[2],
                    "client_ip": params[3],
                    "user_agent": params[4],
                    "created_at": now,
                }
            )
            self._last_row = (now,)

        elif "UPDATE notify_decisions SET status = 'answered'" in norm:
            self.conn.pending_updates.append((params[0], params[1], params[2], int(params[3])))

        elif "SELECT pg_notify(%s, %s)" in norm:
            self.conn.pending_notifies.append((str(params[0]), str(params[1])))

        elif "SELECT id, source_key, decision_key, question, options_json, status, chosen_option, chosen_label, chosen_at, created_at, updated_at FROM notify_decisions WHERE source_key = %s AND decision_key = %s" in norm:
            source_key, dec_key = params[0], params[1]
            with self.conn.cluster.lock:
                found = None
                for d in self.conn.cluster.decisions.values():
                    if d["source_key"] == source_key and d["decision_key"] == dec_key:
                        found = (
                            d["id"],
                            d["source_key"],
                            d["decision_key"],
                            d["question"],
                            d["options_json"],
                            d["status"],
                            d["chosen_option"],
                            d["chosen_label"],
                            d["chosen_at"],
                            d["created_at"],
                            d["updated_at"],
                        )
                        break
                self._last_row = found

        elif "SELECT option_key, created_at FROM notify_decision_events WHERE decision_id = %s" in norm:
            d_id = int(params[0])
            with self.conn.cluster.lock:
                self._last_rowset = [
                    (ev["option_key"], ev["created_at"])
                    for ev in self.conn.cluster.events
                    if ev["decision_id"] == d_id
                ]

        elif "FROM notify_decisions WHERE source_key = %s" in norm:
            source_key = params[0]
            status_filter = params[1] if len(params) == 3 else None
            limit = int(params[-1])
            with self.conn.cluster.lock:
                rows = []
                for d in self.conn.cluster.decisions.values():
                    if d["source_key"] != source_key:
                        continue
                    if status_filter and d["status"] != status_filter:
                        continue
                    rows.append(
                        (
                            d["id"],
                            d["decision_key"],
                            d["question"],
                            d["options_json"],
                            d["status"],
                            d["chosen_option"],
                            d["chosen_label"],
                            d["chosen_at"],
                            d["created_at"],
                        )
                    )
                rows.sort(key=lambda x: (x[8], x[0]), reverse=True)
                self._last_rowset = rows[:limit]

        elif "INSERT INTO notify_outbox" in norm:
            with self.conn.cluster.lock:
                out_id = self.conn.cluster.next_outbox_id
                self.conn.cluster.next_outbox_id += 1
                self.conn.cluster.outbox.append({"id": out_id, "dedup_key": params[0]})
                self._last_row = (out_id,)

        elif "SELECT id, source_key, event_pattern, min_level, channel, target_json, sort_order FROM notify_routes" in norm:
            with self.conn.cluster.lock:
                self._last_rowset = [
                    (
                        r["id"],
                        r["source_key"],
                        r["event_pattern"],
                        r["min_level"],
                        r["channel"],
                        r["target_json"],
                        r["sort_order"],
                    )
                    for r in self.conn.cluster.routes
                ]

        elif "INSERT INTO notify_deliveries" in norm:
            with self.conn.cluster.lock:
                deliv_id = self.conn.cluster.next_delivery_id
                self.conn.cluster.next_delivery_id += 1
                self.conn.cluster.deliveries.append({"id": deliv_id, "status": "pending"})
                self._last_row = (deliv_id,)

        elif "UPDATE notify_deliveries SET status = 'sent'" in norm:
            d_id = int(params[0])
            with self.conn.cluster.lock:
                for d in self.conn.cluster.deliveries:
                    if d["id"] == d_id:
                        d["status"] = "sent"

    def fetchone(self) -> Any:
        return self._last_row

    def fetchall(self) -> Any:
        return self._last_rowset


class FakeSyncConnection:
    """支持事务提交才生效（含 pg_notify 与决策写入）及行锁的伪同步连接。"""

    def __init__(self, cluster: "TransactionalFakePostgres") -> None:
        self.cluster = cluster
        self._holds_row_lock = False
        self.pending_events: list[dict[str, Any]] = []
        self.pending_updates: list[tuple[str, str, datetime, int]] = []
        self.pending_notifies: list[tuple[str, str]] = []

    def acquire_row_lock(self) -> None:
        if not self._holds_row_lock:
            self.cluster.for_update_lock.acquire()
            self._holds_row_lock = True

    def _release_row_lock(self) -> None:
        if self._holds_row_lock:
            self._holds_row_lock = False
            self.cluster.for_update_lock.release()

    def cursor(self) -> FakeSyncCursor:
        return FakeSyncCursor(self)

    def commit(self) -> None:
        try:
            with self.cluster.lock:
                for ev in self.pending_events:
                    ev_id = self.cluster.next_event_id
                    self.cluster.next_event_id += 1
                    ev["id"] = ev_id
                    self.cluster.events.append(ev)
                for opt_key, opt_label, chosen_at, d_id in self.pending_updates:
                    d = self.cluster.decisions.get(d_id)
                    if d is not None:
                        d["status"] = "answered"
                        d["chosen_option"] = opt_key
                        d["chosen_label"] = opt_label
                        d["chosen_at"] = chosen_at
                        d["updated_at"] = datetime.now(timezone.utc)
                notifies = list(self.pending_notifies)
                self.pending_events.clear()
                self.pending_updates.clear()
                self.pending_notifies.clear()
                self.cluster.commits += 1
            for channel, payload in notifies:
                self.cluster.dispatch_notify(channel, payload)
        finally:
            self._release_row_lock()

    def rollback(self) -> None:
        self.pending_events.clear()
        self.pending_updates.clear()
        self.pending_notifies.clear()
        self._release_row_lock()

    def close(self) -> None:
        # 未 commit 的事务在连接关闭时自动回滚并丢弃 pending_notifies
        self.rollback()


class TransactionalFakePostgres:
    """仿真 PostgreSQL：事务提交才派发 NOTIFY、行锁串行化与 LISTEN 连接跟踪。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.for_update_lock = threading.Lock()
        self.sources: dict[str, dict[str, Any]] = {
            "wechat-digest": {
                "token_sha256": hashlib.sha256(b"wechat_tok").hexdigest(),
                "enabled": True,
            },
            "gold-spread-monitor": {
                "token_sha256": hashlib.sha256(b"gold_tok").hexdigest(),
                "enabled": True,
            },
            "other-source": {
                "token_sha256": hashlib.sha256(b"other_tok").hexdigest(),
                "enabled": True,
            },
            "disabled-source": {
                "token_sha256": hashlib.sha256(b"disabled_tok").hexdigest(),
                "enabled": False,
            },
        }
        self.decisions: dict[int, dict[str, Any]] = {}
        self.tokens: list[tuple[int, str]] = []
        self.events: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.deliveries: list[dict[str, Any]] = []
        self.routes: list[dict[str, Any]] = []
        self.next_decision_id = 1
        self.next_event_id = 1
        self.next_outbox_id = 1
        self.next_delivery_id = 1
        self.commits = 0
        self.executed_sqls: list[tuple[str, Any]] = []
        self.dispatched_notifies: list[tuple[str, str]] = []
        self.listeners: list[FakeAsyncListenConnection] = []
        self.open_listener_history: list[FakeAsyncListenConnection] = []
        self.planned_open_failures: list[Exception] = []
        self.listener_opened_event = threading.Event()
        self._next_conn_id = 1

    def sync_connect(self) -> FakeSyncConnection:
        return FakeSyncConnection(self)

    async def async_listen_connect(self, _database_url: str) -> FakeAsyncListenConnection:
        with self.lock:
            if self.planned_open_failures:
                exc = self.planned_open_failures.pop(0)
                raise exc
            conn = FakeAsyncListenConnection(self, self._next_conn_id)
            self._next_conn_id += 1
            self.listeners.append(conn)
            self.open_listener_history.append(conn)
            self.listener_opened_event.set()
            return conn

    def unregister_listener(self, conn: FakeAsyncListenConnection) -> None:
        with self.lock:
            if conn in self.listeners:
                self.listeners.remove(conn)

    def dispatch_notify(self, channel: str, payload: str) -> None:
        with self.lock:
            self.dispatched_notifies.append((channel, payload))
            targets = list(self.listeners)
        for listener in targets:
            listener.push_notify(channel, payload)

    def seed_decision(
        self,
        *,
        source_key: str,
        decision_key: str,
        question: str = "微信掉线，是否重新生成登录二维码？",
        options: list[dict[str, str]] | None = None,
        token: str = "card_tok_1",
        created_at: datetime | None = None,
    ) -> tuple[int, str]:
        with self.lock:
            d_id = self.next_decision_id
            self.next_decision_id += 1
            now = created_at or datetime.now(timezone.utc)
            self.decisions[d_id] = {
                "id": d_id,
                "source_key": source_key,
                "decision_key": decision_key,
                "question": question,
                "options_json": options
                or [
                    {"key": "regen", "label": "重新生成二维码", "style": "primary"},
                    {"key": "skip", "label": "跳过本次", "style": "default"},
                ],
                "status": "pending",
                "chosen_option": None,
                "chosen_label": None,
                "chosen_at": None,
                "created_at": now,
                "updated_at": now,
            }
            self.tokens.append((d_id, store.token_digest(token)))
            return d_id, token


class RecoveryRealtimeTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_backend_app()
        self._env_backup = dict(os.environ)
        self.db = TransactionalFakePostgres()
        self.app = FastAPI()
        self.app.include_router(notify_router.router)
        self.client = TestClient(self.app)
        os.environ["DATABASE_URL"] = "postgresql://test:test@127.0.0.1:5432/test"
        os.environ["NOTIFY_DECISION_STREAM_HEARTBEAT_SECONDS"] = "0.06"
        os.environ["NOTIFY_DECISION_STREAM_RECONNECT_INITIAL_SECONDS"] = "0.02"
        os.environ["NOTIFY_DECISION_STREAM_RECONNECT_MAX_SECONDS"] = "0.05"
        os.environ["NOTIFY_FEISHU_RECOVERY_GATEWAY_TOKEN"] = "gw-secret-token"
        os.environ["NOTIFY_FEISHU_RECOVERY_CHAT_ID"] = "oc_recovery_chat"
        os.environ["NOTIFY_FEISHU_RECOVERY_OPERATORS"] = "ou_bot_op1,ou_bot_op2"

        self.wechat_headers = {
            "X-Notify-Source": "wechat-digest",
            "X-Notify-Token": "wechat_tok",
        }
        self.gold_headers = {
            "X-Notify-Source": "gold-spread-monitor",
            "X-Notify-Token": "gold_tok",
        }
        self.other_headers = {
            "X-Notify-Source": "other-source",
            "X-Notify-Token": "other_tok",
        }
        self.feishu_headers = {
            "X-Notify-Feishu-Action-Token": "gw-secret-token",
        }

        self._orig_open_listen = decision_realtime.open_listen_connection
        self._patches = [
            mock.patch.object(notify_router, "_conn", side_effect=self.db.sync_connect),
            mock.patch.object(dispatch, "_conn", side_effect=self.db.sync_connect),
            mock.patch.object(
                notify_router, "_database_url", side_effect=lambda: os.environ.get("DATABASE_URL", "")
            ),
            mock.patch.object(
                decision_realtime, "open_listen_connection", side_effect=self.db.async_listen_connect
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        decision_realtime._active_streams.clear()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _start_uvicorn(self) -> tuple[uvicorn.Server, threading.Thread, socket.socket, str]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(16)
        sock.setblocking(False)
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            timeout_graceful_shutdown=0.2,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("uvicorn server did not start in time")
            time.sleep(0.01)
        return server, thread, sock, f"http://127.0.0.1:{port}"

    @staticmethod
    def _stop_uvicorn(
        server: uvicorn.Server, thread: threading.Thread, sock: socket.socket
    ) -> None:
        server.should_exit = True
        thread.join(timeout=3.0)
        sock.close()

    @staticmethod
    def _read_sse_events(lines_iter, count: int, *, include_comments: bool = False) -> list[dict[str, str]]:
        collected: list[dict[str, str]] = []
        event_name = ""
        data_lines: list[str] = []
        for raw_line in lines_iter:
            line = raw_line.rstrip("\r\n")
            if line.startswith(":"):
                if include_comments:
                    collected.append({"comment": line[1:].strip()})
                    if len(collected) >= count:
                        break
                continue
            if line == "":
                if event_name or data_lines:
                    collected.append({"event": event_name, "data": "\n".join(data_lines)})
                    event_name = ""
                    data_lines = []
                    if len(collected) >= count:
                        break
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        return collected

    def test_confirmation_post_emits_transactional_notify_only_for_wechat_recovery(self) -> None:
        d_id, tok = self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="wechat-login:drop101:1",
        )
        # GET 只读不触发 NOTIFY
        get_resp = self.client.get(f"/v1/notify/decisions/{d_id}?t={tok}&opt=regen")
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(self.db.dispatched_notifies, [])

        # POST 成功提交后同事务触发 NOTIFY，载荷只含来源名
        post_resp = self.client.post(f"/v1/notify/decisions/{d_id}", data={"t": tok, "opt": "regen"})
        self.assertEqual(post_resp.status_code, 200)
        self.assertIn("已记录：重新生成二维码", post_resp.text)
        self.assertEqual(
            self.db.dispatched_notifies,
            [(decision_realtime.DECISION_WAKE_CHANNEL, "wechat-digest")],
        )
        # 确保通道载荷不含掉线 ID / 尝试号 / 选项 / 验证码
        self.assertNotIn("drop101", self.db.dispatched_notifies[0][1])
        self.assertNotIn("regen", self.db.dispatched_notifies[0][1])

        detail = self.client.get(
            "/v1/internal/notify/decisions/by-key",
            params={"key": "wechat-login:drop101:1"},
            headers=self.wechat_headers,
        ).json()
        self.assertEqual(detail["status"], "answered")
        self.assertEqual(detail["chosen_option"], "regen")
        self.assertEqual([h["option"] for h in detail["history"]], ["regen"])

    def test_uncommitted_or_rolled_back_choice_never_emits_notify(self) -> None:
        d_id, _ = self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="wechat-login:drop102:1",
        )
        conn = self.db.sync_connect()
        decision = store.get_decision_by_id(conn, d_id)
        assert decision is not None
        store.record_decision_choice_with_wake(
            conn,
            decision=decision,
            option_key="regen",
            option_label="重新生成二维码",
        )
        # 提交前监听器未收到信号
        self.assertEqual(self.db.dispatched_notifies, [])
        # 回滚后队列中的 pg_notify 被丢弃
        conn.rollback()
        conn.close()
        self.assertEqual(self.db.dispatched_notifies, [])
        self.assertEqual(self.db.decisions[d_id]["status"], "pending")

    def test_ordinary_and_non_recovery_decisions_keep_semantics_without_notify(self) -> None:
        # 旧普通决策（gold-spread-monitor）
        gold_id, gold_tok = self.db.seed_decision(
            source_key="gold-spread-monitor",
            decision_key="batch-1:swap-1",
            question="是否保留策略？",
            options=[
                {"key": "keep", "label": "保留", "style": "primary"},
                {"key": "rollback", "label": "回退", "style": "danger"},
            ],
        )
        # wechat-digest 非 QR 恢复 key
        non_qr_id, non_qr_tok = self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="wechat-digest:daily-summary",
            question="日报是否重发？",
            options=[
                {"key": "regen", "label": "重发", "style": "primary"},
                {"key": "skip", "label": "跳过", "style": "default"},
            ],
        )
        # wechat-digest 合法 QR key 但非 regen|skip 选项
        other_opt_id, other_opt_tok = self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="wechat-login:drop103:1",
            options=[
                {"key": "pause", "label": "暂停", "style": "default"},
                {"key": "skip", "label": "跳过", "style": "default"},
            ],
        )

        self.assertEqual(
            self.client.post(f"/v1/notify/decisions/{gold_id}", data={"t": gold_tok, "opt": "keep"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(f"/v1/notify/decisions/{gold_id}", data={"t": gold_tok, "opt": "rollback"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(f"/v1/notify/decisions/{non_qr_id}", data={"t": non_qr_tok, "opt": "regen"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(f"/v1/notify/decisions/{other_opt_id}", data={"t": other_opt_tok, "opt": "pause"}).status_code,
            200,
        )
        self.assertEqual(self.db.dispatched_notifies, [])

        gold_detail = self.client.get(
            "/v1/internal/notify/decisions/by-key",
            params={"key": "batch-1:swap-1"},
            headers=self.gold_headers,
        ).json()
        self.assertEqual(gold_detail["chosen_option"], "rollback")
        self.assertEqual([h["option"] for h in gold_detail["history"]], ["keep", "rollback"])

    def test_stream_auth_and_invalid_db_connection_fail_closed(self) -> None:
        # 缺凭据 / 错凭据 / 禁用来源 -> 401
        self.assertEqual(self.client.get("/v1/internal/notify/decisions/stream").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/v1/internal/notify/decisions/stream",
                headers={"X-Notify-Source": "wechat-digest", "X-Notify-Token": "wrong"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/v1/internal/notify/decisions/stream",
                headers={"X-Notify-Source": "disabled-source", "X-Notify-Token": "disabled_tok"},
            ).status_code,
            401,
        )

        # 初始 PG 连接或 LISTEN 失败 -> 503（不返回 200 假流）
        self.db.planned_open_failures.append(RuntimeError("pg unreachable"))
        resp = self.client.get("/v1/internal/notify/decisions/stream", headers=self.wechat_headers)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(decision_realtime.active_stream_count(), 0)

        # 空 DATABASE_URL 调用真实的 open_listen_connection -> 503
        os.environ["DATABASE_URL"] = ""
        with mock.patch.object(
            decision_realtime,
            "open_listen_connection",
            new=self._orig_open_listen,
        ):
            resp_empty = self.client.get("/v1/internal/notify/decisions/stream", headers=self.wechat_headers)
            self.assertEqual(resp_empty.status_code, 503)

    def test_sse_ready_wake_early_commit_realtime_notify_heartbeat_and_other_source(self) -> None:
        # 早于 LISTEN 的提交：先完成一次确认页提交，此时还没有任何 SSE 客户端连接
        d_id, tok = self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="wechat-login:early1:1",
        )
        self.assertEqual(
            self.client.post(f"/v1/notify/decisions/{d_id}", data={"t": tok, "opt": "skip"}).status_code,
            200,
        )

        # Seed 下一次尝试的决策（模拟断开重连后的当前决策）
        d_id2, tok2 = self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="wechat-login:early1:2",
        )

        server, thread, sock, base_url = self._start_uvicorn()
        try:
            with httpx.Client(timeout=5.0) as http:
                with http.stream(
                    "GET",
                    f"{base_url}/v1/internal/notify/decisions/stream",
                    headers=self.wechat_headers,
                ) as stream_resp:
                    self.assertEqual(stream_resp.status_code, 200)
                    self.assertIn("text/event-stream", stream_resp.headers.get("content-type", ""))
                    self.assertEqual(stream_resp.headers.get("x-accel-buffering"), "no")
                    lines = stream_resp.iter_lines()

                    # 1. 连接就绪立即收到 event: wake 和 data: {}
                    first = self._read_sse_events(lines, 1)
                    self.assertEqual(first, [{"event": "wake", "data": "{}"}])
                    # 校验 LISTEN 已以 autocommit 执行在岗
                    self.assertEqual(len(self.db.open_listener_history), 1)
                    self.assertIn(
                        decision_realtime.DECISION_WAKE_CHANNEL,
                        self.db.open_listener_history[0].listening_channels,
                    )
                    # 客户端拿到就绪 wake 后查 by-key，能看到早于 LISTEN 的提交
                    early_detail = http.get(
                        f"{base_url}/v1/internal/notify/decisions/by-key",
                        params={"key": "wechat-login:early1:1"},
                        headers=self.wechat_headers,
                    ).json()
                    self.assertEqual(early_detail["chosen_option"], "skip")

                    # 2. 空闲只收心跳，不收 wake
                    hb = self._read_sse_events(lines, 1, include_comments=True)
                    self.assertEqual(hb, [{"comment": "heartbeat"}])

                    # 3. 注入其他来源 NOTIFY（以及提交普通决策），wechat-digest 流不得触发 wake
                    self.db.dispatch_notify(decision_realtime.DECISION_WAKE_CHANNEL, "other-source")
                    self.db.dispatch_notify(decision_realtime.DECISION_WAKE_CHANNEL, "")
                    self.db.dispatch_notify("unrelated_channel", "wechat-digest")
                    hb2 = self._read_sse_events(lines, 1, include_comments=True)
                    self.assertEqual(hb2, [{"comment": "heartbeat"}])

                    # 4. 同来源选择提交（确认页 POST）实时推送 wake
                    post2 = http.post(
                        f"{base_url}/v1/notify/decisions/{d_id2}",
                        data={"t": tok2, "opt": "regen"},
                    )
                    self.assertEqual(post2.status_code, 200)
                    wakes2 = self._read_sse_events(lines, 1)
                    self.assertEqual(wakes2, [{"event": "wake", "data": "{}"}])

                    # 5. 同来源选择提交（原生 feishu-choice）实时推送 wake
                    d_id3, _ = self.db.seed_decision(
                        source_key="wechat-digest",
                        decision_key="wechat-login:early1:3",
                    )
                    fc = http.post(
                        f"{base_url}/v1/internal/notify/decisions/feishu-choice",
                        json={
                            "decision_key": "wechat-login:early1:3",
                            "option_key": "skip",
                            "operator_open_id": "ou_bot_op1",
                            "chat_id": "oc_recovery_chat",
                        },
                        headers=self.feishu_headers,
                    )
                    self.assertEqual(fc.status_code, 200)
                    wakes3 = self._read_sse_events(lines, 1)
                    self.assertEqual(wakes3, [{"event": "wake", "data": "{}"}])
                    self.assertEqual(self.db.decisions[d_id3]["chosen_option"], "skip")

                # 客户端断开连接后应立即关闭并释放 LISTEN 连接
                deadline = time.monotonic() + 2.0
                while not self.db.open_listener_history[0].closed and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(self.db.open_listener_history[0].closed)
                self.assertEqual(decision_realtime.active_stream_count(), 0)
        finally:
            self._stop_uvicorn(server, thread, sock)

    def test_sse_other_source_stream_does_not_receive_wechat_digest_wake(self) -> None:
        d_id, tok = self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="wechat-login:iso1:1",
        )
        server, thread, sock, base_url = self._start_uvicorn()
        try:
            with httpx.Client(timeout=5.0) as http:
                with http.stream(
                    "GET",
                    f"{base_url}/v1/internal/notify/decisions/stream",
                    headers=self.other_headers,
                ) as stream_resp:
                    self.assertEqual(stream_resp.status_code, 200)
                    lines = stream_resp.iter_lines()
                    first = self._read_sse_events(lines, 1)
                    self.assertEqual(first, [{"event": "wake", "data": "{}"}])

                    # 提交 wechat-digest 决策后，other-source 流只能收到心跳，收不到 wake
                    self.assertEqual(
                        http.post(
                            f"{base_url}/v1/notify/decisions/{d_id}",
                            data={"t": tok, "opt": "regen"},
                        ).status_code,
                        200,
                    )
                    hb = self._read_sse_events(lines, 1, include_comments=True)
                    self.assertEqual(hb, [{"comment": "heartbeat"}])
        finally:
            self._stop_uvicorn(server, thread, sock)

    def test_sse_listen_drop_triggers_real_reconnect_and_wake_on_recovery(self) -> None:
        server, thread, sock, base_url = self._start_uvicorn()
        try:
            with httpx.Client(timeout=5.0) as http:
                with http.stream(
                    "GET",
                    f"{base_url}/v1/internal/notify/decisions/stream",
                    headers=self.wechat_headers,
                ) as stream_resp:
                    self.assertEqual(stream_resp.status_code, 200)
                    lines = stream_resp.iter_lines()
                    self.assertEqual(self._read_sse_events(lines, 1), [{"event": "wake", "data": "{}"}])
                    conn1 = self.db.open_listener_history[0]

                    # 安排一次重连失败后再成功，模拟断线重连退避与恢复
                    self.db.planned_open_failures.append(ConnectionError("transient pg outage"))
                    conn1.inject_error(ConnectionError("pg tcp reset"))

                    # 重连成功后必须补发 wake 以覆盖断线期间的提交
                    reconnected_wake = self._read_sse_events(lines, 1)
                    self.assertEqual(reconnected_wake, [{"event": "wake", "data": "{}"}])
                    self.assertTrue(conn1.closed)
                    self.assertEqual(len(self.db.open_listener_history), 2)
                    conn2 = self.db.open_listener_history[1]
                    self.assertFalse(conn2.closed)
                    self.assertIn(decision_realtime.DECISION_WAKE_CHANNEL, conn2.listening_channels)

                    # 新 LISTEN 连接可继续实时接收后续唤醒
                    self.db.dispatch_notify(decision_realtime.DECISION_WAKE_CHANNEL, "wechat-digest")
                    self.assertEqual(self._read_sse_events(lines, 1), [{"event": "wake", "data": "{}"}])

                    # 若连接静默标记 closed=True 且无异常，也必须触发真实重连与唤醒
                    conn2.closed = True
                    reconnected_wake2 = self._read_sse_events(lines, 1)
                    self.assertEqual(reconnected_wake2, [{"event": "wake", "data": "{}"}])
                    self.assertEqual(len(self.db.open_listener_history), 3)
        finally:
            self._stop_uvicorn(server, thread, sock)

    def test_server_shutdown_releases_active_stream_and_listen_connection(self) -> None:
        server, thread, sock, base_url = self._start_uvicorn()
        received_first = threading.Event()
        stream_ended = threading.Event()

        def consume() -> None:
            try:
                with httpx.Client(timeout=5.0) as http:
                    with http.stream(
                        "GET",
                        f"{base_url}/v1/internal/notify/decisions/stream",
                        headers=self.wechat_headers,
                    ) as resp:
                        lines = resp.iter_lines()
                        self._read_sse_events(lines, 1)
                        received_first.set()
                        for _ in lines:
                            pass
            except Exception:
                pass
            finally:
                stream_ended.set()

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        try:
            self.assertTrue(received_first.wait(timeout=3.0))
            self.assertEqual(decision_realtime.active_stream_count(), 1)
            conn = self.db.open_listener_history[0]
            self.assertFalse(conn.closed)
        finally:
            self._stop_uvicorn(server, thread, sock)

        self.assertTrue(stream_ended.wait(timeout=3.0))
        self.assertTrue(self.db.open_listener_history[0].closed)
        self.assertEqual(decision_realtime.active_stream_count(), 0)

    def test_router_lifespan_shutdown_hook_closes_registered_streams(self) -> None:
        async def run_case() -> None:
            conn = await self.db.async_listen_connect("postgresql://test/test")
            await conn.execute(f"LISTEN {decision_realtime.DECISION_WAKE_CHANNEL}")
            handle = decision_realtime.register_stream(conn)
            stream = decision_realtime.decision_wake_stream(
                source_key="wechat-digest",
                database_url="postgresql://test/test",
                initial_conn=conn,
                handle=handle,
                heartbeat_interval=5.0,
                reconnect_initial=0.02,
                reconnect_max=0.05,
            )

            collected: list[str] = []

            async def drain() -> None:
                try:
                    async for chunk in stream:
                        collected.append(chunk)
                except asyncio.CancelledError:
                    pass

            task = asyncio.create_task(drain())
            await asyncio.sleep(0.02)
            self.assertEqual(collected, [decision_realtime.WAKE_SSE_FRAME])
            self.assertEqual(decision_realtime.active_stream_count(), 1)
            self.assertFalse(conn.closed)

            # 模拟路由 lifespan 关闭钩子触发释放
            async with notify_router._notify_lifespan(self.app):
                pass
            await asyncio.wait_for(task, timeout=1.0)
            self.assertTrue(conn.closed)
            self.assertEqual(decision_realtime.active_stream_count(), 0)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(run_case())
            return
        errors: list[BaseException] = []

        def _runner() -> None:
            try:
                asyncio.run(run_case())
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        t = threading.Thread(target=_runner)
        t.start()
        t.join(timeout=5.0)
        if errors:
            raise errors[0]

    def test_feishu_choice_fail_closed_authorization_and_exact_schema(self) -> None:
        self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="wechat-login:drop201:1",
        )
        valid_body = {
            "decision_key": "wechat-login:drop201:1",
            "option_key": "regen",
            "operator_open_id": "ou_bot_op1",
            "chat_id": "oc_recovery_chat",
        }

        # 1. 精确字段校验：缺字段 / 额外字段 / 空白字段 -> 422
        self.assertEqual(
            self.client.post(
                "/v1/internal/notify/decisions/feishu-choice",
                json={**valid_body, "extra_field": "x"},
                headers=self.feishu_headers,
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/v1/internal/notify/decisions/feishu-choice",
                json={"decision_key": "wechat-login:drop201:1", "option_key": "regen"},
                headers=self.feishu_headers,
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/v1/internal/notify/decisions/feishu-choice",
                json={**valid_body, "chat_id": "   "},
                headers=self.feishu_headers,
            ).status_code,
            422,
        )

        # 2. 独立服务间凭据 fail-closed（不得接受来源 token）
        self.assertEqual(
            self.client.post("/v1/internal/notify/decisions/feishu-choice", json=valid_body).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/v1/internal/notify/decisions/feishu-choice",
                json=valid_body,
                headers={"X-Notify-Feishu-Action-Token": "wechat_tok"},
            ).status_code,
            401,
        )
        with mock.patch.dict(os.environ, {"NOTIFY_FEISHU_RECOVERY_GATEWAY_TOKEN": ""}):
            self.assertEqual(
                self.client.post(
                    "/v1/internal/notify/decisions/feishu-choice",
                    json=valid_body,
                    headers=self.feishu_headers,
                ).status_code,
                503,
            )

        # 3. 群白名单与操作者白名单未配置或通配符 * 时 fail-closed
        for bad_env in (
            {"NOTIFY_FEISHU_RECOVERY_CHAT_ID": ""},
            {"NOTIFY_FEISHU_RECOVERY_CHAT_ID": "*"},
            {"NOTIFY_FEISHU_RECOVERY_OPERATORS": ""},
            {"NOTIFY_FEISHU_RECOVERY_OPERATORS": "*"},
        ):
            with mock.patch.dict(os.environ, bad_env):
                self.assertEqual(
                    self.client.post(
                        "/v1/internal/notify/decisions/feishu-choice",
                        json=valid_body,
                        headers=self.feishu_headers,
                    ).status_code,
                    503,
                )

        # 4. 未授权群或未授权操作者 -> 403
        self.assertEqual(
            self.client.post(
                "/v1/internal/notify/decisions/feishu-choice",
                json={**valid_body, "chat_id": "oc_stranger_chat"},
                headers=self.feishu_headers,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/v1/internal/notify/decisions/feishu-choice",
                json={**valid_body, "operator_open_id": "ou_stranger"},
                headers=self.feishu_headers,
            ).status_code,
            403,
        )

        # 确保失败尝试完全未修改数据库或产生 NOTIFY
        self.assertEqual(self.db.decisions[1]["status"], "pending")
        self.assertEqual(self.db.events, [])
        self.assertEqual(self.db.dispatched_notifies, [])

    def test_feishu_choice_scope_currency_revision_and_concurrent_idempotency(self) -> None:
        # 没有 QR 决策时 -> 404
        self.assertEqual(
            self.client.post(
                "/v1/internal/notify/decisions/feishu-choice",
                json={
                    "decision_key": "wechat-login:drop301:1",
                    "option_key": "regen",
                    "operator_open_id": "ou_bot_op1",
                    "chat_id": "oc_recovery_chat",
                },
                headers=self.feishu_headers,
            ).status_code,
            404,
        )

        # 非 QR 决策 key（即便是其他来源或同来源非 QR）-> 403
        self.db.seed_decision(source_key="gold-spread-monitor", decision_key="gold:other-1")
        self.db.seed_decision(source_key="wechat-digest", decision_key="digest:non-qr")
        for non_qr_key in ("gold:other-1", "digest:non-qr", "wechat-login:onlytwo"):
            self.assertEqual(
                self.client.post(
                    "/v1/internal/notify/decisions/feishu-choice",
                    json={
                        "decision_key": non_qr_key,
                        "option_key": "regen",
                        "operator_open_id": "ou_bot_op1",
                        "chat_id": "oc_recovery_chat",
                    },
                    headers=self.feishu_headers,
                ).status_code,
                403,
            )

        # 依次产生第 1、第 2 次尝试（中间还夹着一条非 QR 决策）
        t0 = datetime(2026, 11, 26, 8, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 11, 26, 8, 1, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 11, 26, 8, 2, 0, tzinfo=timezone.utc)
        self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="wechat-login:drop301:1",
            created_at=t0,
        )
        d2_id, d2_tok = self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="wechat-login:drop301:2",
            created_at=t1,
        )
        self.db.seed_decision(
            source_key="wechat-digest",
            decision_key="digest:newer-non-qr",
            created_at=t2,
        )

        # 旧尝试 wechat-login:drop301:1 已被替代 -> 409
        stale_resp = self.client.post(
            "/v1/internal/notify/decisions/feishu-choice",
            json={
                "decision_key": "wechat-login:drop301:1",
                "option_key": "regen",
                "operator_open_id": "ou_bot_op1",
                "chat_id": "oc_recovery_chat",
            },
            headers=self.feishu_headers,
        )
        self.assertEqual(stale_resp.status_code, 409)
        self.assertEqual(self.db.dispatched_notifies, [])

        # 未知选项 -> 422
        bad_opt_resp = self.client.post(
            "/v1/internal/notify/decisions/feishu-choice",
            json={
                "decision_key": "wechat-login:drop301:2",
                "option_key": "unknown_opt",
                "operator_open_id": "ou_bot_op1",
                "chat_id": "oc_recovery_chat",
            },
            headers=self.feishu_headers,
        )
        self.assertEqual(bad_opt_resp.status_code, 422)

        # 并发重投当前 QR 决策相同 option（模拟超时重试/双点击）-> 仅写 1 条历史、1 次 NOTIFY
        payload = {
            "decision_key": "wechat-login:drop301:2",
            "option_key": "regen",
            "operator_open_id": "ou_bot_op2",
            "chat_id": "oc_recovery_chat",
        }

        def send_once() -> httpx.Response:
            local_client = TestClient(self.app)
            return local_client.post(
                "/v1/internal/notify/decisions/feishu-choice",
                json=payload,
                headers=self.feishu_headers,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: send_once(), range(4)))

        recorded = []
        for resp in responses:
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            recorded.append(payload.pop("newly_recorded"))
            self.assertEqual(payload, {"ok": True, "decision_key": "wechat-login:drop301:2", "option_key": "regen"})
        self.assertEqual(recorded.count(True), 1)
        self.assertEqual(len(self.db.events), 1)
        self.assertEqual(len(self.db.dispatched_notifies), 1)

        # 同一当前决策通过确认页改选为 skip，走同一持久化路径并保留完整历史
        revise = self.client.post(f"/v1/notify/decisions/{d2_id}", data={"t": d2_tok, "opt": "skip"})
        self.assertEqual(revise.status_code, 200)
        detail = self.client.get(
            "/v1/internal/notify/decisions/by-key",
            params={"key": "wechat-login:drop301:2"},
            headers=self.wechat_headers,
        ).json()
        self.assertEqual(detail["chosen_option"], "skip")
        self.assertEqual([h["option"] for h in detail["history"]], ["regen", "skip"])
        self.assertEqual(len(self.db.dispatched_notifies), 2)


IT_DATABASE_URL = os.getenv("NOTIFY_DECISION_INTEGRATION_DATABASE_URL", "").strip()


@unittest.skipUnless(
    IT_DATABASE_URL and psycopg is not None,
    "NOTIFY_DECISION_INTEGRATION_DATABASE_URL not set",
)
class RecoveryRealtimePostgresIntegrationTests(unittest.TestCase):
    """在真实 PostgreSQL 上验证同事务 NOTIFY、LISTEN 重连与行锁并发。"""

    def setUp(self) -> None:
        _ensure_backend_app()
        self._env_backup = dict(os.environ)
        self.source = "wechat-digest"
        self.token = uuid.uuid4().hex
        self.other_source = f"it-other-{uuid.uuid4().hex[:8]}"
        self.other_token = uuid.uuid4().hex
        with psycopg.connect(IT_DATABASE_URL) as conn:
            conn.execute(
                """
                INSERT INTO notify_sources (source_key, token_sha256, enabled, note)
                VALUES (%s, %s, true, 'it recovery realtime')
                ON CONFLICT (source_key) DO UPDATE SET token_sha256 = EXCLUDED.token_sha256, enabled = true
                """,
                (self.source, hashlib.sha256(self.token.encode()).hexdigest()),
            )
            conn.execute(
                """
                INSERT INTO notify_sources (source_key, token_sha256, enabled, note)
                VALUES (%s, %s, true, 'it recovery other')
                """,
                (self.other_source, hashlib.sha256(self.other_token.encode()).hexdigest()),
            )
        os.environ["DATABASE_URL"] = IT_DATABASE_URL
        os.environ["NOTIFY_DECISION_STREAM_HEARTBEAT_SECONDS"] = "0.2"
        os.environ["NOTIFY_FEISHU_RECOVERY_GATEWAY_TOKEN"] = "it-gw-token"
        os.environ["NOTIFY_FEISHU_RECOVERY_CHAT_ID"] = "oc_it_chat"
        os.environ["NOTIFY_FEISHU_RECOVERY_OPERATORS"] = "ou_it_op"

        self.app = FastAPI()
        self.app.include_router(notify_router.router)
        self.client = TestClient(self.app)
        connect = lambda: psycopg.connect(IT_DATABASE_URL)  # noqa: E731
        self._patches = [
            mock.patch.object(notify_router, "_conn", side_effect=connect),
            mock.patch.object(dispatch, "_conn", side_effect=connect),
            mock.patch.object(notify_router, "_database_url", return_value=IT_DATABASE_URL),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_real_postgres_notify_and_sse_stream_end_to_end(self) -> None:
        drop_id = uuid.uuid4().hex[:10]
        decision_key = f"wechat-login:{drop_id}:1"
        send_resp = self.client.post(
            "/v1/internal/notify/send",
            json={
                "source": "wechat-digest",
                "event": "wechat.login_required",
                "level": "warn",
                "title": "微信恢复测试",
                "summary": "请确认",
                "dedup_key": f"it:{drop_id}:1",
                "decision": {
                    "key": decision_key,
                    "question": "是否重新生成二维码？",
                    "options": [
                        {"key": "regen", "label": "重新生成", "style": "primary"},
                        {"key": "skip", "label": "跳过", "style": "default"},
                    ],
                },
            },
            headers={"X-Notify-Source": "wechat-digest", "X-Notify-Token": self.token},
        )
        self.assertEqual(send_resp.status_code, 200, send_resp.text)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(16)
        sock.setblocking(False)
        server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            while not server.started:
                time.sleep(0.01)
            with httpx.Client(timeout=5.0) as http:
                with http.stream(
                    "GET",
                    f"http://127.0.0.1:{port}/v1/internal/notify/decisions/stream",
                    headers={"X-Notify-Source": "wechat-digest", "X-Notify-Token": self.token},
                ) as resp:
                    self.assertEqual(resp.status_code, 200)
                    lines = resp.iter_lines()
                    first = RecoveryRealtimeTests._read_sse_events(lines, 1)
                    self.assertEqual(first, [{"event": "wake", "data": "{}"}])

                    choice_resp = http.post(
                        f"http://127.0.0.1:{port}/v1/internal/notify/decisions/feishu-choice",
                        json={
                            "decision_key": decision_key,
                            "option_key": "regen",
                            "operator_open_id": "ou_it_op",
                            "chat_id": "oc_it_chat",
                        },
                        headers={"X-Notify-Feishu-Action-Token": "it-gw-token"},
                    )
                    self.assertEqual(choice_resp.status_code, 200, choice_resp.text)
                    wakes = RecoveryRealtimeTests._read_sse_events(lines, 1)
                    self.assertEqual(wakes, [{"event": "wake", "data": "{}"}])
        finally:
            server.should_exit = True
            thread.join(timeout=3.0)
            sock.close()


    def test_telegram_card_advances_only_after_same_group_image_receipt(self) -> None:
        from app.notify import recovery_cards
        from app.notify.models import Notification

        key = f"telegram-login:{uuid.uuid4().hex[:10]}:1"
        target = '{"profile":"TEST","receive_id":"oc_sample","receive_id_type":"chat_id"}'
        payload = {
            "source": "wechat-digest", "event": "wechat.telegram.retry",
            "level": "warn", "title": "二维码过期", "summary": "确认发新码",
            "decision": {"key": key, "question": "是否发新码？", "options": [
                {"key": "regen", "label": "重新生成", "style": "primary"},
                {"key": "skip", "label": "暂不处理", "style": "default"},
            ]},
            "dedup_key": key + ":retry",
        }
        with psycopg.connect(IT_DATABASE_URL) as conn:
            self.assertEqual(self.client.post("/v1/internal/notify/send", json=payload, headers={
                "X-Notify-Source": "wechat-digest", "X-Notify-Token": self.token,
            }).status_code, 200)
            response = self.client.post("/v1/internal/notify/decisions/feishu-choice", json={
                "decision_key": key, "option_key": "regen", "operator_open_id": "ou_it_op", "chat_id": "oc_it_chat",
            }, headers={"X-Notify-Feishu-Action-Token": "it-gw-token"})
            self.assertEqual(response.status_code, 200, response.text)
            conn.execute("UPDATE notify_decisions SET chosen_at = now() - interval '10 seconds' WHERE decision_key = %s", (key,))
            row = conn.execute("SELECT id FROM notify_outbox WHERE dedup_key = %s", (key + ":retry",)).fetchone()
            delivery = conn.execute(
                "INSERT INTO notify_deliveries(outbox_id, channel, target_json, status) VALUES(%s, 'feishu', %s::jsonb, 'sent') RETURNING id",
                (row[0], target),
            ).fetchone()[0]
            recovery_cards.record_receipt(conn, delivery, Notification.model_validate(payload), {"message_id": "om_retry"})
            conn.commit()
            with mock.patch.object(recovery_cards.feishu, "update_recovery_card") as patch:
                recovery_cards.refresh_cards(conn, 5)
                patch.assert_any_call("TEST", "om_retry", "Telegram 登录恢复", "正在生成新二维码")
                qr_payload = dict(payload, event="wechat.telegram.qr", decision=None, dedup_key=key[:-1] + "2:qr")
                qr_row = conn.execute(
                    "INSERT INTO notify_outbox(source_key,event,level,dedup_key,payload_json) VALUES('wechat-digest','wechat.telegram.qr','warn',%s,'{}') RETURNING id",
                    (qr_payload["dedup_key"],),
                ).fetchone()[0]
                qr_delivery = conn.execute(
                    "INSERT INTO notify_deliveries(outbox_id, channel, target_json, status) VALUES(%s, 'feishu', %s::jsonb, 'sent') RETURNING id",
                    (qr_row, target),
                ).fetchone()[0]
                qr = Notification.model_validate(qr_payload)
                recovery_cards.record_receipt(conn, qr_delivery, qr, {"message_id": "om_qr", "qr_attached": False})
                conn.commit()
                patch.reset_mock()
                recovery_cards.refresh_cards(conn, 5)
                self.assertFalse(any(call.args[1] == "om_retry" for call in patch.call_args_list))
                recovery_cards.record_receipt(conn, qr_delivery, qr, {"message_id": "om_qr", "qr_attached": True})
                conn.commit()
                recovery_cards.refresh_cards(conn, 5)
                patch.assert_any_call("TEST", "om_retry", "Telegram 登录恢复", "新二维码已发出，请扫描最新一张。")


if __name__ == "__main__":
    unittest.main()
