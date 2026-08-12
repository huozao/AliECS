from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.recipes.bom_query import locate_recipe_source


ERROR_KIND_LABELS = {
    "auth": "凭据过期",
    "rate_limit": "请求限流",
    "network": "网络异常",
    "schema": "数据结构变化",
    "write": "写入失败",
    "unknown": "未知错误",
}


def error_kind_label(error_kind: str | None) -> str:
    return ERROR_KIND_LABELS.get(str(error_kind or "unknown"), "未知错误")


def classify_freshness(
    last_success_at,
    sla_seconds,
    *,
    now=None,
) -> dict[str, Any]:
    if sla_seconds is None:
        return {
            "state": "unmonitored",
            "sla_seconds": None,
            "age_seconds": None,
            "ratio": None,
        }

    sla = int(sla_seconds)
    if last_success_at is None:
        return {
            "state": "never",
            "sla_seconds": sla,
            "age_seconds": None,
            "ratio": None,
        }

    current = now or datetime.now(timezone.utc)
    elapsed_seconds = max(0.0, (current - last_success_at).total_seconds())
    age = int(elapsed_seconds)
    ratio = elapsed_seconds / sla if sla > 0 else None
    state = (
        "stale"
        if elapsed_seconds > sla
        else ("warning" if elapsed_seconds >= sla * 0.8 else "fresh")
    )
    return {
        "state": state,
        "sla_seconds": sla,
        "age_seconds": age,
        "ratio": ratio,
    }


def formula_bom_artifact() -> dict[str, Any] | None:
    try:
        path = locate_recipe_source()
        stat = path.stat()
    except (FileNotFoundError, OSError):
        return None

    return {
        "name": path.name,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "mtime_epoch": int(stat.st_mtime),
    }


_OVERVIEW_SQL = """
SELECT
    j.job_key,
    j.kind,
    j.provider,
    j.display_name,
    j.enabled,
    j.schedule,
    j.freshness_sla_seconds,
    j.artifact_glob,
    j.alert_enabled,
    latest.id,
    latest.trigger,
    latest.status,
    latest.started_at,
    latest.finished_at,
    latest.row_count,
    latest.changed_count,
    latest.error_kind,
    latest.error_message,
    latest.detail_json,
    latest.legacy_ref,
    succeeded.finished_at,
    COALESCE(alerts.open_alert_count, 0)
FROM sync_jobs j
LEFT JOIN LATERAL (
    SELECT id, trigger, status, started_at, finished_at, row_count, changed_count,
           error_kind, error_message, detail_json, legacy_ref
    FROM sync_job_runs
    WHERE job_id = j.id
    ORDER BY started_at DESC, id DESC
    LIMIT 1
) latest ON TRUE
LEFT JOIN LATERAL (
    SELECT finished_at
    FROM sync_job_runs
    WHERE job_id = j.id AND status = 'success'
    ORDER BY finished_at DESC NULLS LAST, id DESC
    LIMIT 1
) succeeded ON TRUE
LEFT JOIN (
    SELECT job_id, COUNT(*) AS open_alert_count
    FROM sync_job_alerts
    WHERE state = 'open'
    GROUP BY job_id
) alerts ON alerts.job_id = j.id
ORDER BY j.provider ASC, j.display_name ASC, j.job_key ASC
"""


def _last_run(row: tuple[Any, ...]) -> dict[str, Any] | None:
    if row[9] is None:
        return None
    return {
        "id": row[9],
        "trigger": row[10],
        "status": row[11],
        "started_at": row[12],
        "finished_at": row[13],
        "row_count": row[14],
        "changed_count": row[15],
        "error_kind": row[16],
        "error_label": error_kind_label(row[16]),
        "error_message": row[17],
        "detail_json": row[18] or {},
        "legacy_ref": row[19] or {},
    }


def overview(conn, *, now=None) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(_OVERVIEW_SQL)
        rows = cur.fetchall()

    summary = {
        "jobs": len(rows),
        "fresh": 0,
        "warning": 0,
        "stale": 0,
        "never": 0,
        "unmonitored": 0,
        "failed": 0,
        "partial": 0,
        "running": 0,
        "open_alerts": 0,
    }
    items: list[dict[str, Any]] = []
    formula_artifact: dict[str, Any] | None = None
    formula_artifact_loaded = False

    for row in rows:
        freshness = classify_freshness(row[20], row[6], now=now)
        summary[freshness["state"]] += 1
        last_run = _last_run(row)
        if last_run and last_run["status"] in ("failed", "partial", "running"):
            summary[last_run["status"]] += 1
        open_alert_count = int(row[21] or 0)
        summary["open_alerts"] += open_alert_count

        artifact = None
        if row[0] == "chanjet.full":
            if not formula_artifact_loaded:
                formula_artifact = formula_bom_artifact()
                formula_artifact_loaded = True
            artifact = formula_artifact

        items.append(
            {
                "job_key": row[0],
                "kind": row[1],
                "provider": row[2],
                "display_name": row[3],
                "enabled": row[4],
                "schedule": row[5] or {},
                "freshness_sla_seconds": row[6],
                "artifact_glob": row[7],
                "alert_enabled": row[8],
                "last_run": last_run,
                "last_success_at": row[20],
                "freshness": freshness,
                "next_expected_at": None,
                "open_alert_count": open_alert_count,
                "artifact": artifact,
            }
        )

    return {"summary": summary, "items": items}


def alerts_page(
    conn,
    *,
    state: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    if state not in {"open", "resolved", "all"}:
        raise ValueError("unsupported alert state")

    state_where = "" if state == "all" else "WHERE a.state = %s"
    state_params: tuple[Any, ...] = () if state == "all" else (state,)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM sync_job_alerts a {state_where}",
            state_params,
        )
        count_row = cur.fetchone()
        total = int(count_row[0]) if count_row else 0
        cur.execute(
            f"""
            SELECT
                a.id,
                j.job_key,
                j.display_name,
                j.provider,
                a.run_id,
                a.alert_kind,
                a.state,
                a.first_seen_at,
                a.last_notified_at,
                a.notify_count,
                a.resolved_at,
                a.payload_json
            FROM sync_job_alerts a
            JOIN sync_jobs j ON j.id = a.job_id
            {state_where}
            ORDER BY a.first_seen_at DESC, a.id DESC
            LIMIT %s OFFSET %s
            """,
            (*state_params, limit, offset),
        )
        rows = cur.fetchall()

    items = [
        {
            "id": row[0],
            "job_key": row[1],
            "display_name": row[2],
            "provider": row[3],
            "run_id": row[4],
            "alert_kind": row[5],
            "state": row[6],
            "first_seen_at": row[7],
            "last_notified_at": row[8],
            "notify_count": row[9],
            "resolved_at": row[10],
            "payload_json": row[11] or {},
        }
        for row in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


_RUN_COLUMNS = """
    r.id,
    j.job_key,
    j.display_name,
    j.provider,
    j.kind,
    r.trigger,
    r.status,
    r.started_at,
    r.finished_at,
    r.row_count,
    r.changed_count,
    r.error_kind,
    r.error_message,
    r.detail_json,
    r.legacy_ref
"""


def _run_filters(
    *,
    job_key: str | None,
    provider: str | None,
    status: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("j.job_key", job_key),
        ("j.provider", provider),
        ("r.status", status),
    ):
        if value:
            clauses.append(f"{column} = %s")
            params.append(value)
    return (" AND ".join(clauses) or "TRUE"), params


def _duration_seconds(
    started_at,
    finished_at,
    status: str,
    *,
    now=None,
) -> float | None:
    if started_at is None:
        return None
    end = finished_at
    if end is None:
        if status != "running":
            return None
        end = now or datetime.now(timezone.utc)
    return max(0.0, (end - started_at).total_seconds())


def _run_item(row: tuple[Any, ...], *, now=None) -> dict[str, Any]:
    return {
        "id": row[0],
        "job_key": row[1],
        "display_name": row[2],
        "provider": row[3],
        "kind": row[4],
        "trigger": row[5],
        "status": row[6],
        "started_at": row[7],
        "finished_at": row[8],
        "row_count": row[9],
        "changed_count": row[10],
        "error_kind": row[11],
        "error_label": error_kind_label(row[11]),
        "error_message": row[12],
        "detail_json": row[13] or {},
        "legacy_ref": row[14] or {},
        "duration_seconds": _duration_seconds(
            row[7], row[8], row[6], now=now
        ),
    }


def job_exists(conn, job_key: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sync_jobs WHERE job_key = %s",
            (job_key,),
        )
        return cur.fetchone() is not None


def runs_page(
    conn,
    *,
    job_key: str | None,
    provider: str | None,
    status: str | None,
    limit: int,
    offset: int,
    now=None,
) -> dict[str, Any]:
    predicate, filter_params = _run_filters(
        job_key=job_key,
        provider=provider,
        status=status,
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM sync_job_runs r
            JOIN sync_jobs j ON j.id = r.job_id
            WHERE {predicate}
            """,
            tuple(filter_params),
        )
        count_row = cur.fetchone()
        total = int(count_row[0]) if count_row else 0
        cur.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM sync_job_runs r
            JOIN sync_jobs j ON j.id = r.job_id
            WHERE {predicate}
            ORDER BY r.started_at DESC, r.id DESC
            LIMIT %s OFFSET %s
            """,
            (*filter_params, limit, offset),
        )
        rows = cur.fetchall()

    return {
        "items": [_run_item(row, now=now) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def run_detail(conn, run_id: int, *, now=None) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM sync_job_runs r
            JOIN sync_jobs j ON j.id = r.job_id
            WHERE r.id = %s
            """,
            (run_id,),
        )
        run_row = cur.fetchone()
        if run_row is None:
            return None

        cur.execute(
            """
            SELECT seq, name, status, started_at, finished_at, items, message
            FROM sync_job_steps
            WHERE run_id = %s
            ORDER BY seq ASC
            """,
            (run_id,),
        )
        step_rows = cur.fetchall()

        run = _run_item(run_row, now=now)
        reconciliation_id = None
        detail_json = run["detail_json"]
        full_snapshot_id = (
            detail_json.get("full_snapshot_id")
            if isinstance(detail_json, dict)
            else None
        )
        if (
            run["job_key"] == "chanjet.full"
            and type(full_snapshot_id) is int
        ):
            cur.execute(
                """
                SELECT id
                FROM integration_reconciliation_diffs
                WHERE provider = 'chanjet'
                  AND module = 'bom'
                  AND full_snapshot_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (full_snapshot_id,),
            )
            reconciliation_row = cur.fetchone()
            reconciliation_id = (
                reconciliation_row[0] if reconciliation_row else None
            )

    steps = [
        {
            "seq": row[0],
            "name": row[1],
            "status": row[2],
            "started_at": row[3],
            "finished_at": row[4],
            "items": row[5],
            "message": row[6],
            "duration_seconds": _duration_seconds(
                row[3], row[4], row[2], now=now
            ),
        }
        for row in step_rows
    ]
    return {
        "run": run,
        "steps": steps,
        "reconciliation_id": reconciliation_id,
    }
