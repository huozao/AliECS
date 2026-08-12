from __future__ import annotations

import re
from contextlib import closing
from typing import Any, Callable

from tplus_datahub.core.exceptions import ChanjetAPIError
from tplus_datahub.jobs.db_sync_requests import connect_if_configured

try:
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - keeps pure unit tests dependency-light.
    Jsonb = lambda value: value  # type: ignore


_RUN_STATUSES = {"running", "success", "partial", "failed"}
_STEP_STATUSES = {"running", "success", "failed"}
_TPLUS_JOB_KEYS = {"chanjet.full", "tplus.parent_match"}
_LEGACY_TABLES = {"sync_runs", "integration_sync_runs"}
_SECRET_PATTERN = re.compile(
    r"(?i)(?P<key>[\"']?(?:access[_ -]?token|corpsecret|app[_ -]?secret|authorization)[\"']?)"
    r"\s*(?P<separator>[:=])\s*"
    r"(?P<value>[\"'](?:bearer\s+)?[^\"']*[\"']|(?:bearer\s+)?[^\s,;}\]]+)"
)


def classify_error(exc: BaseException | str | None) -> str:
    if exc is None:
        return "unknown"
    if isinstance(exc, ChanjetAPIError):
        if exc.status_code in {401, 403}:
            return "auth"
        if exc.status_code == 429:
            return "rate_limit"
        if exc.status_code is not None and exc.status_code >= 500:
            return "network"
    message = str(exc).lower()
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(
        term in message for term in ("timeout", "timed out", "connection", "network", "dns", "socket")
    ):
        return "network"
    if any(term in message for term in ("access_token", "corpsecret", "app secret", "app_secret", "authorization", "401", "403", "unauthorized", "forbidden", "credential")):
        return "auth"
    if any(term in message for term in ("429", "rate limit", "too many requests", "throttl")):
        return "rate_limit"
    if any(term in message for term in ("schema", "column", "relation", "table", "json", "validation", "invalid input")):
        return "schema"
    if any(term in message for term in ("database", "psycopg", "sql", "integrity", "constraint", "write")):
        return "write"
    return "unknown"


def safe_error_message(exc: BaseException | str | None) -> str:
    message = str(exc or "")
    message = _SECRET_PATTERN.sub(
        lambda match: f"{match.group('key')}{match.group('separator')} [REDACTED]", message
    )
    return message[:500]


def _log_failure(operation: str) -> None:
    try:
        print(f"sync job platform write failed: {operation}")
    except Exception:
        pass


def _best_effort(operation: str, write: Callable[[Any], Any]) -> Any:
    try:
        conn = connect_if_configured()
        if conn is None:
            return None
        with closing(conn):
            try:
                result = write(conn)
                conn.commit()
                return result
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _log_failure(operation)
                return None
    except Exception:
        _log_failure(operation)
        return None


def _validate_legacy_ref(legacy_ref: dict[str, Any]) -> None:
    if legacy_ref == {}:
        return
    if (
        set(legacy_ref) == {"table", "id"}
        and legacy_ref.get("table") in _LEGACY_TABLES
        and type(legacy_ref.get("id")) is int
    ):
        return
    raise ValueError("invalid legacy reference")


def start_run(
    *,
    job_key: str,
    kind: str,
    provider: str,
    display_name: str,
    source_id: int | None,
    trigger: str,
    legacy_ref: dict[str, Any],
) -> int | None:
    def write(conn: Any) -> int:
        if job_key not in _TPLUS_JOB_KEYS or source_id is not None:
            raise ValueError("invalid tplus job source")
        _validate_legacy_ref(legacy_ref)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_jobs(job_key, kind, provider, display_name, source_id, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT(job_key) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    provider = EXCLUDED.provider,
                    display_name = EXCLUDED.display_name,
                    source_id = EXCLUDED.source_id,
                    updated_at = NOW()
                RETURNING id
                """,
                (job_key, kind, provider, display_name, source_id),
            )
            job = cur.fetchone()
            cur.execute(
                """
                INSERT INTO sync_job_runs(job_id, trigger, status, legacy_ref)
                VALUES (%s, %s, 'running', %s)
                RETURNING id
                """,
                (int(job[0]), trigger, Jsonb(legacy_ref)),
            )
            run = cur.fetchone()
        return int(run[0])

    return _best_effort("start_run", write)


def upsert_step(run_id: int, seq: int, name: str, status: str, items: int = 0, message: str = "") -> None:
    def write(conn: Any) -> None:
        if status not in _STEP_STATUSES:
            raise ValueError("invalid step status")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_job_steps(run_id, seq, name, status, started_at, finished_at, items, message)
                VALUES (
                    %s, %s, %s, %s, NOW(),
                    CASE WHEN %s IN ('success', 'failed') THEN NOW() END,
                    %s, %s
                )
                ON CONFLICT (run_id, seq) DO UPDATE SET
                    name = EXCLUDED.name,
                    status = EXCLUDED.status,
                    finished_at = CASE
                        WHEN EXCLUDED.status IN ('success', 'failed') THEN NOW()
                        ELSE sync_job_steps.finished_at
                    END,
                    items = EXCLUDED.items,
                    message = EXCLUDED.message
                """,
                (run_id, seq, name, status, status, items, message),
            )

    _best_effort("upsert_step", write)


def finish_run(
    run_id: int,
    *,
    status: str,
    row_count: int,
    changed_count: int,
    error: BaseException | str | None,
    detail_json: dict[str, Any],
) -> None:
    def write(conn: Any) -> None:
        if status not in _RUN_STATUSES:
            raise ValueError("invalid run status")
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_job_runs
                SET status = %s,
                    finished_at = NOW(),
                    row_count = %s,
                    changed_count = %s,
                    error_kind = %s,
                    error_message = %s,
                    detail_json = %s
                WHERE id = %s
                """,
                (
                    status,
                    row_count,
                    changed_count,
                    classify_error(error) if error else None,
                    safe_error_message(error) if error else None,
                    Jsonb(detail_json),
                    run_id,
                ),
            )

    _best_effort("finish_run", write)


def attach_legacy_ref(platform_run_id: int, legacy_run_id: int) -> None:
    def write(conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_job_runs
                SET legacy_ref = %s
                WHERE id = %s
                """,
                (Jsonb({"table": "integration_sync_runs", "id": legacy_run_id}), platform_run_id),
            )

    _best_effort("attach_legacy_ref", write)
