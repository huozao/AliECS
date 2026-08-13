from __future__ import annotations

import os
from contextlib import closing
from typing import Any

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency guard for local partial installs
    psycopg = None

try:
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover
    Jsonb = lambda value: value  # type: ignore


def attach_legacy_ref(platform_run_id: int, legacy_run_id: int) -> None:
    from tplus_datahub.jobs import sync_job_platform

    sync_job_platform.attach_legacy_ref(platform_run_id, legacy_run_id)


def connect_if_configured() -> Any | None:
    if psycopg is None:
        return None
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return None
    return psycopg.connect(database_url, connect_timeout=3)


# BOM 回写请求与整轮全量请求共用 integration_sync_requests，靠 module 分流：
# 'bom' 是 BOM builder 提交的写回，'all' 是页面上「立即全量同步」排的队。
# 两条队列各取各的，取错会把回写请求当全量跑（或反过来）。
_NEXT_BOM_REQUEST_SQL = """
    SELECT id, mode, target_json, reason_event_id
    FROM integration_sync_requests
    WHERE provider = 'chanjet'
      AND module = 'bom'
      AND status = 'pending'
    ORDER BY priority ASC, requested_at ASC, id ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
    """

_NEXT_FULL_REQUEST_SQL = """
    SELECT id, mode, target_json, reason_event_id
    FROM integration_sync_requests
    WHERE provider = 'chanjet'
      AND module = 'all'
      AND status = 'pending'
    ORDER BY priority ASC, requested_at ASC, id ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
    """


def _fetch_next_request(sql: str, conn: Any | None, limit: int) -> dict[str, Any] | None:
    if conn is None:
        owned_conn = connect_if_configured()
        if owned_conn is None:
            return None
        with closing(owned_conn):
            return _fetch_next_request(sql, owned_conn, limit)

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(sql, ())
            row = cur.fetchone()
            if not row:
                return None
            request = {
                "id": int(row[0]),
                "mode": row[1],
                "target_json": row[2] or {},
                "reason_event_id": row[3],
            }
            cur.execute(
                """
                UPDATE integration_sync_requests
                SET status = 'running', started_at = NOW(), updated_at = NOW()
                WHERE id = %s
                """,
                (request["id"],),
            )
            return request


def fetch_next_bom_request(conn: Any | None = None, limit: int = 5) -> dict[str, Any] | None:
    return _fetch_next_request(_NEXT_BOM_REQUEST_SQL, conn, limit)


def fetch_next_full_request(conn: Any | None = None, limit: int = 5) -> dict[str, Any] | None:
    """页面「立即全量同步」排的队；worker 在睡眠轮询里消费。"""
    return _fetch_next_request(_NEXT_FULL_REQUEST_SQL, conn, limit)


def fetch_sync_config(provider: str = "chanjet", conn: Any | None = None) -> dict[str, Any] | None:
    """读取定时同步配置（enabled / interval_seconds / anchor_time）。无 DB / 无行 / 表不存在
    均返回 None，由调用方回退到 env 默认。"""
    if conn is None:
        owned_conn = connect_if_configured()
        if owned_conn is None:
            return None
        with closing(owned_conn):
            return fetch_sync_config(provider, owned_conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled, interval_seconds, anchor_time FROM integration_sync_config WHERE provider = %s",
            (provider,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "enabled": bool(row[0]),
            "interval_seconds": int(row[1]),
            "anchor_time": str(row[2] or ""),
        }


def fetch_platform_schedule(job_key: str = "chanjet.full", conn: Any | None = None) -> dict[str, Any] | None:
    if conn is None:
        owned_conn = connect_if_configured()
        if owned_conn is None:
            return None
        with closing(owned_conn):
            return fetch_platform_schedule(job_key, owned_conn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT schedule FROM sync_jobs WHERE job_key = %s", (job_key,))
            row = cur.fetchone()
        schedule = row[0] if row else None
        return dict(schedule) if isinstance(schedule, dict) and schedule else None
    except Exception:
        conn.rollback()
        return None


def seed_platform_schedule(schedule: dict[str, Any], job_key: str = "chanjet.full", conn: Any | None = None) -> None:
    if conn is None:
        owned_conn = connect_if_configured()
        if owned_conn is None:
            return
        with closing(owned_conn):
            seed_platform_schedule(schedule, job_key, owned_conn)
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_jobs
                SET schedule = %s, updated_at = NOW()
                WHERE job_key = %s
                  AND schedule = '{}'::jsonb
                """,
                (Jsonb(schedule), job_key),
            )
        conn.commit()
    except Exception:
        conn.rollback()


def record_scheduler_shadow(
    payload: dict[str, Any], job_key: str = "chanjet.full", conn: Any | None = None
) -> list[int]:
    if conn is None:
        owned_conn = connect_if_configured()
        if owned_conn is None:
            return []
        with closing(owned_conn):
            return record_scheduler_shadow(payload, job_key, owned_conn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (r.job_id) r.id
                  FROM sync_job_runs r
                  JOIN sync_jobs j ON j.id = r.job_id
                  WHERE r.trigger = 'schedule'
                    AND j.job_key = %s
                  ORDER BY r.job_id, r.started_at DESC
                )
                UPDATE sync_job_runs r
                SET detail_json = jsonb_set(r.detail_json, '{shadow}', %s, true)
                FROM latest
                WHERE r.id = latest.id
                RETURNING r.id
                """,
                (job_key, Jsonb(payload)),
            )
            run_ids = [int(row[0]) for row in cur.fetchall()]
        conn.commit()
        return run_ids
    except Exception:
        conn.rollback()
        return []


def finish_scheduler_shadow(
    run_ids: list[int], observed_sleep_seconds: int, candidate_would_wake: bool, conn: Any | None = None
) -> None:
    if not run_ids:
        return
    if conn is None:
        owned_conn = connect_if_configured()
        if owned_conn is None:
            return
        with closing(owned_conn):
            finish_scheduler_shadow(run_ids, observed_sleep_seconds, candidate_would_wake, owned_conn)
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_job_runs r
                SET detail_json = jsonb_set(
                    r.detail_json,
                    '{shadow}',
                    COALESCE(r.detail_json -> 'shadow', '{}'::jsonb)
                      || jsonb_build_object(
                          'observed_sleep_seconds', %s::integer,
                          'candidate_would_wake', %s::boolean
                      ),
                    true
                )
                WHERE r.id = ANY(%s)
                """,
                (int(observed_sleep_seconds), bool(candidate_would_wake), run_ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()


def fetch_last_scheduled_full_at(provider: str = "chanjet", conn: Any | None = None) -> Any | None:
    """上一次定时全量同步的开始时刻（aware-UTC）。用于重启后不丢锚点相位：
    容器重建不应该在白天立刻补跑一次全量。无 DB / 无记录返回 None。"""
    if conn is None:
        owned_conn = connect_if_configured()
        if owned_conn is None:
            return None
        with closing(owned_conn):
            return fetch_last_scheduled_full_at(provider, owned_conn)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT started_at FROM integration_sync_runs
            WHERE provider = %s AND mode = 'scheduled_full' AND started_at IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (provider,),
        )
        row = cur.fetchone()
        return row[0] if row else None


# mode 写死 'manual_full'，绝不能是 'scheduled_full'：fetch_last_scheduled_full_at() 按
# mode='scheduled_full' 取锚点相位，手动跑一次若记成定时，worker 会认为"这个周期已经跑过"，
# 当晚锚点那轮直接判未到期被整轮跳过——手动补一次反而顶掉了当天的定时同步。
# module 仍是 'all'：doc-sync 的 tplus_parent_match 事件触发按 module IN ('all','bom')
# AND status='success' 抬水位，手动全量成功后企微「色粉使用记录表 / 标准型号0117」核对要能跟着触发。
_RECORD_FULL_RUN_SQL = """
    INSERT INTO integration_sync_runs(provider, module, mode, status, finished_at, row_count, exit_code, detail_json, error_json)
    VALUES ('chanjet', 'all', 'manual_full', %s, NOW(), 0, %s, %s, %s)
    RETURNING id
    """


def finish_full_request(request_id: int, status: str, exit_code: int, detail: dict[str, Any]) -> None:
    """手动全量跑完的记账：写一条 integration_sync_runs，再回填请求行。"""
    conn = connect_if_configured()
    if conn is None:
        return
    error_json = {"modules": detail.get("failure_details") or []} if detail.get("failure_details") else {}
    with closing(conn):
        with conn.cursor() as cur:
            cur.execute(
                _RECORD_FULL_RUN_SQL,
                (status, exit_code, Jsonb(detail), Jsonb(error_json)),
            )
            run_id = int(cur.fetchone()[0])
            cur.execute(
                """
                UPDATE integration_sync_requests
                SET status = %s,
                    finished_at = NOW(),
                    sync_run_id = %s,
                    error_json = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (status, run_id, Jsonb(error_json), request_id),
            )
        conn.commit()
    platform_run_id = detail.get("platform_run_id")
    if platform_run_id is not None:
        try:
            attach_legacy_ref(int(platform_run_id), run_id)
        except Exception:  # noqa: BLE001 - legacy run/request 已提交，挂接只是附加可观测性
            print("[tplus] sync platform legacy attach failed")


def finish_bom_request(request_id: int, status: str, exit_code: int, detail: dict[str, Any]) -> None:
    conn = connect_if_configured()
    if conn is None:
        return
    with closing(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO integration_sync_runs(provider, module, mode, status, finished_at, exit_code, detail_json, error_json)
                VALUES ('chanjet', 'bom', %s, %s, NOW(), %s, %s, %s)
                RETURNING id
                """,
                (
                    str(detail.get("mode") or "incremental"),
                    status,
                    exit_code,
                    Jsonb(detail),
                    Jsonb({} if status == "success" else detail),
                ),
            )
            run_id = int(cur.fetchone()[0])
            cur.execute(
                """
                UPDATE integration_sync_requests
                SET status = %s,
                    finished_at = NOW(),
                    sync_run_id = %s,
                    error_json = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (status, run_id, Jsonb({} if status == "success" else detail), request_id),
            )
        conn.commit()
