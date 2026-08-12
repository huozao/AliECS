from __future__ import annotations

import re
from typing import Any, Callable

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # pragma: no cover - pure unit tests do not require psycopg.
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value: Any) -> None:
            self.value = value


_RUN_STATUSES = {"running", "success", "partial", "failed"}
_STEP_STATUSES = {"running", "success", "failed"}
_LEGACY_TABLES = {"sync_runs", "integration_sync_runs"}
_TPLUS_JOB_METADATA = {
    "chanjet.full": ("pull", "chanjet"),
    "tplus.parent_match": ("reconcile", "chanjet"),
}
_DOCUMENT_JOB_KEY = re.compile(r"^(wecom|feishu)\.doc\.(\d+)$")
_SECRET_PATTERN = re.compile(
    r"(?i)(?P<key>[\"']?(?:access[_ -]?token|corpsecret|app[_ -]?secret|authorization)[\"']?)"
    r"\s*(?P<separator>[:=])\s*"
    r"(?P<value>[\"'](?:bearer\s+)?[^\"']*[\"']|(?:bearer\s+)?[^\s,;}\]]+)"
)


def classify_error(exc: BaseException | str | None) -> str:
    """Map platform errors to the fixed vocabulary consumed by UI and alerts."""
    if exc is None:
        return "unknown"
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


class SyncJobPlatformWriter:
    def __init__(self, conn: Any, logger: Callable[[str], None] = print, owns_connection: bool = False) -> None:
        self.conn = conn
        self.logger = logger
        self.owns_connection = owns_connection

    def _best_effort(self, operation: str, write: Callable[[], Any]) -> Any:
        try:
            return write()
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            try:
                self.logger(f"sync job platform write failed: {operation}")
            except Exception:
                pass
            return None

    @staticmethod
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

    @staticmethod
    def _validate_start(
        job_key: str,
        kind: str,
        provider: str,
        source_id: int | None,
        legacy_ref: dict[str, Any],
    ) -> None:
        SyncJobPlatformWriter._validate_legacy_ref(legacy_ref)
        document_job = _DOCUMENT_JOB_KEY.fullmatch(job_key)
        if document_job:
            if kind != "pull" or provider != document_job.group(1):
                raise ValueError("invalid document metadata")
            if type(source_id) is not int or source_id <= 0 or int(document_job.group(2)) != source_id:
                raise ValueError("invalid document source")
            return
        expected_metadata = _TPLUS_JOB_METADATA.get(job_key)
        if expected_metadata is not None:
            if source_id is not None:
                raise ValueError("invalid tplus source")
            if (kind, provider) != expected_metadata:
                raise ValueError("invalid tplus metadata")
            return
        raise ValueError("unknown job key")

    def start_run(
        self,
        *,
        job_key: str,
        kind: str,
        provider: str,
        display_name: str,
        source_id: int | None,
        trigger: str,
        legacy_ref: dict[str, Any],
    ) -> int | None:
        def write() -> int:
            self._validate_start(job_key, kind, provider, source_id, legacy_ref)
            with self.conn.cursor() as cur:
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
            self.conn.commit()
            return int(run[0])

        return self._best_effort("start_run", write)

    def upsert_step(
        self, run_id: int, seq: int, name: str, status: str, *, items: int = 0, message: str = ""
    ) -> None:
        def write() -> None:
            if status not in _STEP_STATUSES:
                raise ValueError("invalid step status")
            with self.conn.cursor() as cur:
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
            self.conn.commit()

        self._best_effort("upsert_step", write)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        row_count: int,
        changed_count: int,
        error: BaseException | str | None,
        detail_json: dict[str, Any],
    ) -> None:
        def write() -> None:
            if status not in _RUN_STATUSES:
                raise ValueError("invalid run status")
            with self.conn.cursor() as cur:
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
            self.conn.commit()

        self._best_effort("finish_run", write)

    @classmethod
    def open_owned(cls, logger: Callable[[str], None] = print) -> "SyncJobPlatformWriter":
        from app.storage.postgres import connect

        return cls(connect(), logger=logger, owns_connection=True)

    def close(self) -> None:
        if self.owns_connection:
            self.conn.close()


class _NoopSyncJobPlatformWriter:
    def start_run(self, **_: Any) -> None:
        return None

    def upsert_step(self, *_: Any, **__: Any) -> None:
        return None

    def finish_run(self, *_: Any, **__: Any) -> None:
        return None

    def close(self) -> None:
        return None


def platform_writer_for(store: Any) -> SyncJobPlatformWriter | _NoopSyncJobPlatformWriter:
    return getattr(store, "sync_jobs", _NoopSyncJobPlatformWriter())


def open_owned(logger: Callable[[str], None] = print) -> SyncJobPlatformWriter:
    return SyncJobPlatformWriter.open_owned(logger=logger)
