"""worker 侧的消息中枢客户端。

worker 与 backend-api 是两个镜像、构建上下文互不可见，没法共享 Python 包，所以这里
**只写库**：往 notify_outbox 插一行就算交付，投递由 backend-api 那一份代码负责。
好处是 worker 不依赖 backend-api 活着；代价是 payload 的字段格式成了跨镜像的隐式契约。

⚠️ 这个契约由 tests/test_notify_center.py::CrossServiceContractTests 守着——
改这里的 payload 结构，必须同步改 backend-api 的 app/notify/models.py 并跑那个测试，
否则 worker 写进去的消息会在投递侧解析失败，而且失败是异步的、当场看不出来。
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # pragma: no cover - 纯单测环境没有 psycopg
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value: Any) -> None:
            self.value = value


def build_payload(
    *,
    source: str,
    event: str,
    title: str,
    summary: str = "",
    level: str = "info",
    text_segments: list[str] | None = None,
    fields: list[tuple[str, str]] | None = None,
    link: tuple[str, str] | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """构造与 backend-api Notification 对齐的 payload。

    worker 侧只发文字和键值对——它没有图要发，所以这里不提供 image 段。
    """
    segments: list[dict[str, Any]] = []
    if fields:
        segments.append(
            {"kind": "fields", "fields": [{"name": name, "value": value} for name, value in fields]}
        )
    for text in text_segments or []:
        if text.strip():
            segments.append({"kind": "text", "text": text})
    payload: dict[str, Any] = {
        "source": source,
        "event": event,
        "level": level,
        "title": title,
        "summary": summary,
        "segments": segments,
        "images": [],
        "occurred_at": (occurred_at or datetime.now(timezone.utc)).isoformat(),
    }
    if link is not None:
        payload["link"] = {"text": link[0], "url": link[1]}
    return payload


def default_dedup_key(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {key: payload.get(key) for key in ("source", "event", "title", "summary", "segments")},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"auto:{payload.get('source')}:{digest[:32]}"


def connect() -> Any:
    """惰性拿连接。

    模块级不 import app.storage 是有意的：backend-api 侧的契约测试要按路径加载本模块，
    而那个进程里 sys.modules['app'] 是 backend 的同名包，模块级 import 会当场失败。
    """
    from app.storage.postgres import connect as _connect

    return _connect()


def enqueue(payload: dict[str, Any], *, dedup_key: str = "", conn: Any = None) -> bool:
    """写 outbox。返回是否新建（False = 这个 dedup_key 已经进过队）。

    失败不抛异常：通知发不出去不该让调用它的同步作业算失败——
    这是四处旧实现共同的既有行为，收敛时保持不变。
    """
    key = dedup_key or default_dedup_key(payload)
    payload = dict(payload)
    payload["dedup_key"] = key
    owns_conn = conn is None
    try:
        connection = conn or connect()
    except Exception as exc:  # noqa: BLE001
        print(f"[notify] 连接数据库失败，通知丢弃：{type(exc).__name__}")
        return False
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notify_outbox (dedup_key, source_key, event, level, payload_json)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (dedup_key) DO NOTHING
                RETURNING id
                """,
                (
                    key,
                    payload.get("source", ""),
                    payload.get("event", ""),
                    payload.get("level", "info"),
                    Jsonb(payload),
                ),
            )
            created = cur.fetchone() is not None
        connection.commit()
        return created
    except Exception as exc:  # noqa: BLE001
        print(f"[notify] 写 outbox 失败，通知丢弃：{type(exc).__name__}")
        return False
    finally:
        if owns_conn:
            try:
                connection.close()
            except Exception:
                pass


def request_flush(timeout: int = 10) -> None:
    """请 backend-api 带走积压。

    这是「尽力而为」的一脚油门：调不通也无所谓，行已经落库了，下一轮再带。
    worker 主循环每轮调一次，所以 worker 写的通知最长延迟一个轮询周期。
    """
    base = (os.getenv("NOTIFY_FLUSH_URL") or "").strip()
    token = (os.getenv("NOTIFY_FLUSH_TOKEN") or "").strip()
    if not (base and token):
        return
    request = urllib.request.Request(
        base,
        data=b"",
        headers={"X-Notify-Flush-Token": token, "Content-Length": "0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return
    except Exception as exc:  # noqa: BLE001
        print(f"[notify] flush 触发失败（不影响已落库的通知）：{type(exc).__name__}")
