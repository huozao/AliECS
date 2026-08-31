"""notify_* 四张表的读写。这里只碰数据库，不碰任何 IM API。"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from app.notify.models import Notification, level_at_least

# 重试退避：第 n 次失败后等 BACKOFF_SECONDS[n-1]，四档全部用完才判 dead。
# ⚠️ 索引是 n-1 不是 n。2026-08-31 修：原来写 BACKOFF_SECONDS[attempts]，而 attempts
# 在取值前已自增，于是第一次失败等的是 300 秒而不是 60 秒，列表首档 60 永远取不到。
# 生产实测过这个偏差（outbox 10：失败到重投间隔 300s）。判据必须断言**具体秒数**，
# 只断言 status=='pending' 在正确和错误索引下都成立，测不出任何东西。
BACKOFF_SECONDS = [60, 300, 1800, 7200]
# 首投 + 四次重试 = 5 次尝试，第 5 次仍失败判 dead。
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_source_token(conn, source_key: str, token: str) -> bool:
    """来源 + token 校验。用 compare_digest 防时序侧信道。"""
    if not (source_key and token):
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT token_sha256 FROM notify_sources WHERE source_key = %s AND enabled",
            (source_key,),
        )
        row = cur.fetchone()
    if not row:
        return False
    return hmac.compare_digest(str(row[0]), token_digest(token))


def enqueue(conn, notification: Notification) -> tuple[int, bool]:
    """写 outbox。返回 (outbox_id, 是否新建)。

    dedup_key 撞上已有行时不报错也不覆盖——重复提交是正常现象（生产者重试、
    网络重发），第二次只是拿到同一个 outbox_id 和 is_new=False。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notify_outbox (dedup_key, source_key, event, level, payload_json)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (dedup_key) DO NOTHING
            RETURNING id
            """,
            (
                notification.dedup_key,
                notification.source,
                notification.event,
                notification.level,
                Jsonb(notification.storable_payload()),
            ),
        )
        row = cur.fetchone()
        if row is not None:
            return int(row[0]), True
        cur.execute("SELECT id FROM notify_outbox WHERE dedup_key = %s", (notification.dedup_key,))
        existing = cur.fetchone()
    return int(existing[0]), False


def matching_routes(conn, source_key: str, event: str, level: str) -> list[dict[str, Any]]:
    """匹配路由。source_key 与 event_pattern 都支持 glob（'*' 即全部）。

    过滤放在 Python 而不是 SQL：event_pattern 是 glob 不是 LIKE，而且路由表规模
    只有几十行，读全表再筛比在 SQL 里拼模式匹配更好读也更好测。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source_key, event_pattern, min_level, channel, target_json, sort_order
            FROM notify_routes
            WHERE enabled
            ORDER BY sort_order, id
            """
        )
        rows = cur.fetchall()
    matched: list[dict[str, Any]] = []
    for row in rows:
        route = dict(
            zip(
                ["id", "source_key", "event_pattern", "min_level", "channel", "target_json", "sort_order"],
                row,
            )
        )
        if not fnmatch.fnmatchcase(source_key, str(route["source_key"])):
            continue
        if not fnmatch.fnmatchcase(event, str(route["event_pattern"])):
            continue
        if not level_at_least(level, str(route["min_level"])):
            continue
        matched.append(route)
    return matched


def create_deliveries(conn, outbox_id: int, routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为每个命中的 target 建一条投递记录。重复调用不会重复建（唯一键兜住）。"""
    created: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for route in routes:
            cur.execute(
                """
                INSERT INTO notify_deliveries (outbox_id, route_id, channel, target_json)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (outbox_id, route_id) DO NOTHING
                RETURNING id
                """,
                (outbox_id, route["id"], route["channel"], Jsonb(route["target_json"])),
            )
            row = cur.fetchone()
            if row is not None:
                created.append(
                    {
                        "id": int(row[0]),
                        "channel": route["channel"],
                        "target_json": route["target_json"],
                        "attempts": 0,
                    }
                )
    return created


def delivery_summary(conn, outbox_id: int) -> dict[str, int]:
    """汇总一条 outbox 当前的实际投递状态，供重复提交和查询接口复用。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, COUNT(*)
            FROM notify_deliveries
            WHERE outbox_id = %s
            GROUP BY status
            """,
            (outbox_id,),
        )
        rows = cur.fetchall()
    counts = {str(status): int(count) for status, count in rows}
    sent = counts.get("sent", 0)
    pending = counts.get("pending", 0)
    dead = counts.get("dead", 0)
    return {
        "targets": sum(counts.values()),
        "sent": sent,
        "pending": pending,
        "dead": dead,
        "failed": pending + dead,
    }


def delivery_receipt(conn, outbox_id: int, source_key: str) -> dict[str, Any] | None:
    """读取属于指定来源的投递凭证；不返回 target_json 或任何渠道凭据引用。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, dedup_key, source_key, event, level, created_at
            FROM notify_outbox
            WHERE id = %s AND source_key = %s
            """,
            (outbox_id, source_key),
        )
        outbox = cur.fetchone()
        if outbox is None:
            return None
        cur.execute(
            """
            SELECT id, channel, status, attempts, last_error, next_attempt_at, sent_at
            FROM notify_deliveries
            WHERE outbox_id = %s
            ORDER BY id
            """,
            (outbox_id,),
        )
        rows = cur.fetchall()
    deliveries = [
        {
            "delivery_id": int(row[0]),
            "channel": str(row[1]),
            "status": str(row[2]),
            "attempts": int(row[3]),
            "last_error": str(row[4] or ""),
            "next_attempt_at": row[5],
            "sent_at": row[6],
        }
        for row in rows
    ]
    counts = {"sent": 0, "pending": 0, "dead": 0}
    for delivery in deliveries:
        status = str(delivery["status"])
        if status in counts:
            counts[status] += 1
    return {
        "outbox_id": int(outbox[0]),
        "dedup_key": str(outbox[1]),
        "source": str(outbox[2]),
        "event": str(outbox[3]),
        "level": str(outbox[4]),
        "created_at": outbox[5],
        "targets": len(deliveries),
        "sent": counts["sent"],
        "pending": counts["pending"],
        "dead": counts["dead"],
        "deliveries": deliveries,
    }


def mark_unroutable(conn, outbox_id: int) -> bool:
    """给「一条路由都没命中」的 outbox 行留一条墓碑投递记录。

    没有这一步，`delivered:false / no matching route` 的行会**永远**满足
    claim_orphans 的「有 outbox 行但没有 deliveries 行」，于是：

    1. runbook 的孤儿判据永久报阳性，而那句话写的是「持续有行 = flush 没在跑」——
       实际 flush 好好的，判据会把人引向完全错误的方向；
    2. flush 每轮（约 30 秒）重新领养它一次、建不出记录、再记一条
       `matched no route`，永远空转下去；
    3. runbook 说「发出去没有只看 notify_deliveries」，可这张表里偏偏什么都没有，
       「没配路由」和「还没投递」在观测面上长得一模一样。

    2026-08-31 端到端验证 /v1/internal/notify/send 时实测到（outbox 7）。
    墓碑行 status='dead' 所以 claim_pending 不会捡它，channel='none' 也不会被
    sender_for 解析——它只是让「没人会收到」这件事在该看的表里留下痕迹。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notify_deliveries
                (outbox_id, route_id, channel, target_json, status, attempts, last_error)
            SELECT %s, NULL, 'none', '{}'::jsonb, 'dead', 0, 'no matching route'
            WHERE NOT EXISTS (SELECT 1 FROM notify_deliveries WHERE outbox_id = %s)
            RETURNING id
            """,
            (outbox_id, outbox_id),
        )
        return cur.fetchone() is not None


def mark_sent(conn, delivery_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE notify_deliveries
            SET status = 'sent', sent_at = now(), last_error = '', attempts = attempts + 1
            WHERE id = %s
            """,
            (delivery_id,),
        )


def mark_failed(conn, delivery_id: int, error: str) -> str:
    """记一次失败，安排下一次重试；退避档位用完则判 dead。返回新状态。"""
    with conn.cursor() as cur:
        cur.execute("SELECT attempts FROM notify_deliveries WHERE id = %s", (delivery_id,))
        row = cur.fetchone()
        attempts = int(row[0]) + 1 if row else 1
        if attempts >= MAX_ATTEMPTS:
            status, delay = "dead", 0
        else:
            status, delay = "pending", BACKOFF_SECONDS[attempts - 1]
        cur.execute(
            """
            UPDATE notify_deliveries
            SET status = %s, attempts = %s, last_error = %s, next_attempt_at = %s
            WHERE id = %s
            """,
            (
                status,
                attempts,
                error[:500],
                datetime.now(timezone.utc) + timedelta(seconds=delay),
                delivery_id,
            ),
        )
    return status


def claim_orphans(conn, limit: int = 50) -> list[dict[str, Any]]:
    """取还没有任何投递记录的 outbox 行。

    doc-sync-worker 只写 outbox、不建 deliveries——它不读路由表，也不该读（路由是
    投递侧的事）。所以 worker 写的行落库时是「孤儿」，必须由 flush 领养：匹配路由、
    建投递记录、再投递。

    ⚠️ 少了这一步，worker 写的通知会安全落库然后永远发不出去，而且没有任何
    失败信号——outbox 有行、deliveries 没行、flush 报 claimed=0，三处都「正常」。
    2026-08-31 上线自检时就是这么暴露的。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.id, o.source_key, o.event, o.level, o.payload_json
            FROM notify_outbox o
            LEFT JOIN notify_deliveries d ON d.outbox_id = o.id
            WHERE d.id IS NULL
            ORDER BY o.id
            LIMIT %s
            FOR UPDATE OF o SKIP LOCKED
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "outbox_id": int(row[0]),
            "source_key": row[1],
            "event": row[2],
            "level": row[3],
            "payload": row[4],
        }
        for row in rows
    ]


def claim_pending(conn, limit: int = 50) -> list[dict[str, Any]]:
    """取一批到期的待投递记录，连同它们的 outbox payload。

    用 FOR UPDATE SKIP LOCKED：backend-api 可能有多个副本，两个 flush 撞在一起时
    各取各的，不会把同一条投两遍。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.channel, d.target_json, d.attempts, o.payload_json
            FROM notify_deliveries d
            JOIN notify_outbox o ON o.id = d.outbox_id
            WHERE d.status = 'pending' AND d.next_attempt_at <= now()
            ORDER BY d.next_attempt_at
            LIMIT %s
            FOR UPDATE OF d SKIP LOCKED
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": int(row[0]),
            "channel": row[1],
            "target_json": row[2],
            "attempts": int(row[3]),
            "payload": row[4],
        }
        for row in rows
    ]
