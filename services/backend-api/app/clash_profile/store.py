"""clash_profile_snapshots 的读写。

单独成模块是因为有两个调用方：HTTP 路由（后台按钮）和 cli.py（本机每日同步任务
通过 docker exec 调用，不经过 HTTP，因而不受 SSO 影响）。
"""

from __future__ import annotations

from contextlib import closing
from typing import Any

from app.clash_profile.fetch import Snapshot
from app.core import _conn


_STATUS_COLUMNS = (
    "provider_id, node_count, fingerprint, userinfo, fetched_at, changed_at, last_error, last_error_at"
)


def read_status() -> dict[int, dict[str, Any]]:
    """各订阅源的快照状态，**不含 content**——状态接口不该把节点凭据带进响应。"""
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_STATUS_COLUMNS} FROM clash_profile_snapshots")
            rows = cur.fetchall()
    return {
        row[0]: {
            "provider_id": row[0],
            "node_count": row[1],
            "fingerprint": row[2],
            "userinfo": row[3],
            "fetched_at": row[4].isoformat() if row[4] else None,
            "changed_at": row[5].isoformat() if row[5] else None,
            "last_error": row[6],
            "last_error_at": row[7].isoformat() if row[7] else None,
        }
        for row in rows
    }


def read_content(provider_id: int) -> str | None:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content FROM clash_profile_snapshots WHERE provider_id = %s", (provider_id,)
            )
            row = cur.fetchone()
    return row[0] if row else None


def save_snapshot(provider_id: int, snapshot: Snapshot) -> bool:
    """写入快照，返回指纹是否发生了变化（True 表示客户端需要重新导入配置）。

    changed_at 只在指纹变化时推进；fetched_at 每次成功都推进。两者分开是为了让后台
    能区分"今天拉过了"和"节点真的变了"——机场每天下发的流量数字都在变，但那不算变更。
    """
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fingerprint FROM clash_profile_snapshots WHERE provider_id = %s",
                (provider_id,),
            )
            row = cur.fetchone()
            changed = row is None or row[0] != snapshot.fingerprint
            cur.execute(
                """
                INSERT INTO clash_profile_snapshots
                    (provider_id, content, node_count, fingerprint, userinfo,
                     fetched_at, changed_at, last_error, last_error_at)
                VALUES (%s, %s, %s, %s, %s, now(), now(), '', NULL)
                ON CONFLICT (provider_id) DO UPDATE SET
                    content = EXCLUDED.content,
                    node_count = EXCLUDED.node_count,
                    fingerprint = EXCLUDED.fingerprint,
                    userinfo = EXCLUDED.userinfo,
                    fetched_at = now(),
                    changed_at = CASE
                        WHEN clash_profile_snapshots.fingerprint = EXCLUDED.fingerprint
                        THEN clash_profile_snapshots.changed_at
                        ELSE now()
                    END,
                    last_error = '',
                    last_error_at = NULL
                """,
                (
                    provider_id,
                    snapshot.content,
                    snapshot.node_count,
                    snapshot.fingerprint,
                    snapshot.userinfo,
                ),
            )
        conn.commit()
    return changed


def save_error(provider_id: int, message: str) -> None:
    """只记录失败，不动 content —— 拉取失败时客户端仍应拿到上一份可用节点。

    没有历史快照时也要落一行，否则后台看不到"从来没成功过"这个状态。
    """
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO clash_profile_snapshots
                    (provider_id, content, node_count, fingerprint, fetched_at,
                     changed_at, last_error, last_error_at)
                VALUES (%s, '', 0, '', NULL, NULL, %s, now())
                ON CONFLICT (provider_id) DO UPDATE SET
                    last_error = EXCLUDED.last_error,
                    last_error_at = now()
                """,
                (provider_id, message[:2000]),
            )
        conn.commit()
