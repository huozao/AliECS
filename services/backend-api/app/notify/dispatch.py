"""投递编排：路由匹配 → 建投递记录 → 实际发送 → 记账重试。

投递策略是「同步优先 + 失败落队」：HTTP 入口收到消息时当场发完，调用方立刻知道
成没成（沿用 gold_spread_alerts 的既有行为，外部设备的体验不变）；发失败的留在
notify_deliveries 里，由 flush 带走重试。

刻意不开常驻投递进程：backend-api 是 FastAPI 多副本，doc-sync-worker 的主循环
本来就在跑（默认 30s 一轮），让它每轮捎一次 flush 比新增一个守护进程便宜得多。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core import _conn
from app.notify import store
from app.notify.channels import sender_for
from app.notify.models import Notification

logger = logging.getLogger("aliecs.notify")


def _attempt(conn, delivery: dict[str, Any], notification: Notification) -> str:
    """发一条。返回 'sent' / 'pending'（待重试）/ 'dead'（重试用尽）。"""
    try:
        sender_for(str(delivery["channel"]))(notification, dict(delivery["target_json"] or {}))
    except Exception as exc:  # noqa: BLE001 - 任何投递失败都只是这一条的事，不该炸掉整批
        reason = f"{type(exc).__name__}: {exc}"
        status = store.mark_failed(conn, int(delivery["id"]), reason)
        logger.warning(
            "notify delivery failed id=%s channel=%s status=%s reason=%s",
            delivery["id"], delivery["channel"], status, reason[:200],
        )
        return status
    store.mark_sent(conn, int(delivery["id"]))
    return "sent"


def deliver(notification: Notification, *, conn=None) -> dict[str, Any]:
    """入队并立即投递。

    返回 {"outbox_id", "duplicate", "targets", "sent", "failed"}。
    ``duplicate=True`` 表示这个 dedup_key 之前已经进过队——此时不会重复投递。
    ``targets=0`` 表示没有任何路由命中：消息安全落库了，但没人会收到，
    调用方应当把它当成配置缺失而不是发送成功。
    """
    owns_conn = conn is None
    connection = conn or _conn()
    try:
        outbox_id, is_new = store.enqueue(connection, notification)
        if not is_new:
            connection.commit()
            return {"outbox_id": outbox_id, "duplicate": True, "targets": 0, "sent": 0, "failed": 0}

        routes = store.matching_routes(
            connection, notification.source, notification.event, notification.level
        )
        deliveries = store.create_deliveries(connection, outbox_id, routes)
        connection.commit()

        sent = failed = 0
        for delivery in deliveries:
            # 每条投递单独提交：发送是外部 IO，不该把它圈在一个长事务里。
            if _attempt(connection, delivery, notification) == "sent":
                sent += 1
            else:
                failed += 1
            connection.commit()

        if not routes:
            logger.warning(
                "notify has no matching route source=%s event=%s level=%s",
                notification.source, notification.event, notification.level,
            )
        return {
            "outbox_id": outbox_id,
            "duplicate": False,
            "targets": len(deliveries),
            "sent": sent,
            "failed": failed,
        }
    finally:
        if owns_conn:
            connection.close()


def flush(limit: int = 50, *, conn=None) -> dict[str, Any]:
    """重投一批到期的失败记录。

    ⚠️ 从库里读回来的消息**没有图片**——图片字节在入库时就被剥掉了（见
    Notification.storable_payload）。所以重试出去的是无图版本：字还在，图没了。
    首次投递才有图，这是为了不让 payload_json 被几十万字符的 base64 撑爆。
    """
    owns_conn = conn is None
    connection = conn or _conn()
    try:
        pending = store.claim_pending(connection, limit=limit)
        sent = failed = dead = 0
        for delivery in pending:
            try:
                notification = Notification.from_stored(dict(delivery["payload"] or {}))
            except Exception as exc:  # noqa: BLE001 - 坏 payload 直接判死，不然它会一直卡在队首
                store.mark_failed(connection, int(delivery["id"]), f"bad payload: {type(exc).__name__}")
                connection.commit()
                dead += 1
                continue
            status = _attempt(connection, delivery, notification)
            connection.commit()
            if status == "sent":
                sent += 1
            elif status == "dead":
                dead += 1
            else:
                failed += 1
        return {"claimed": len(pending), "sent": sent, "failed": failed, "dead": dead}
    finally:
        if owns_conn:
            connection.close()
