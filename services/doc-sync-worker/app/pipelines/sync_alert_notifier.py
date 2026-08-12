from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # pragma: no cover - pure unit tests do not require psycopg.
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value: Any) -> None:
            self.value = value


ERROR_KIND_LABELS = {
    "auth": "凭据过期",
    "rate_limit": "请求限流",
    "network": "网络异常",
    "schema": "数据结构变化",
    "write": "写入失败",
    "unknown": "未知错误",
}
TOKEN_ALERT_THRESHOLD_SECONDS = 4 * 86400
ARTIFACT_GRACE_SECONDS = 300

_EVENT_TITLES = {
    "open": "同步告警",
    "escalate": "同步告警升级",
    "resolved": "同步已恢复",
}
_ALERT_KIND_LABELS = {
    "failed": "同步失败",
    "stale": "同步延迟",
    "credential": "凭据告警",
}


def error_kind_label(error_kind: str | None) -> str:
    return ERROR_KIND_LABELS[_normalized_error_kind(error_kind)]


def _normalized_error_kind(error_kind: object) -> str:
    candidate = str(error_kind or "unknown")
    if candidate in ERROR_KIND_LABELS:
        return candidate
    return "unknown"


def _credential_result(
    *,
    configured: bool,
    ok: bool,
    expired: bool,
    expires_at: datetime | None,
    remaining_hours: float | None,
    message: str,
) -> dict[str, Any]:
    return {
        "configured": configured,
        "ok": ok,
        "expired": expired,
        "expires_at": expires_at,
        "remaining_hours": remaining_hours,
        "message": message,
    }


def _decode_jwt_expiration(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload_part = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_part.encode("ascii")))
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None
        expiration_epoch = float(exp)
        if not math.isfinite(expiration_epoch):
            return None
        datetime.fromtimestamp(expiration_epoch, tz=timezone.utc)
        return expiration_epoch
    except (OSError, OverflowError, UnicodeEncodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def credential_status(token_path: str, *, now: datetime) -> dict[str, Any]:
    path = Path(token_path)
    if not path.is_file():
        return _credential_result(
            configured=False, ok=False, expired=False, expires_at=None, remaining_hours=None, message="未配置凭据"
        )
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return _credential_result(
            configured=True, ok=False, expired=False, expires_at=None, remaining_hours=None, message="凭据不可读取"
        )
    if not token:
        return _credential_result(
            configured=True, ok=False, expired=False, expires_at=None, remaining_hours=None, message="凭据内容为空"
        )

    expiration_epoch = _decode_jwt_expiration(token)
    if expiration_epoch is None:
        return _credential_result(
            configured=True, ok=False, expired=False, expires_at=None, remaining_hours=None, message="凭据格式无效"
        )

    expires_at = datetime.fromtimestamp(expiration_epoch, tz=timezone.utc)
    remaining_seconds = expiration_epoch - now.timestamp()
    remaining_hours = round(remaining_seconds / 3600, 2)
    if remaining_seconds <= 0:
        return _credential_result(
            configured=True, ok=False, expired=True, expires_at=expires_at, remaining_hours=remaining_hours, message="凭据已过期"
        )
    if remaining_seconds <= TOKEN_ALERT_THRESHOLD_SECONDS:
        return _credential_result(
            configured=True, ok=False, expired=False, expires_at=expires_at, remaining_hours=remaining_hours, message="凭据即将到期"
        )
    return _credential_result(
        configured=True, ok=True, expired=False, expires_at=expires_at, remaining_hours=remaining_hours, message="凭据有效"
    )


def artifact_is_stale(last_success_started_at, artifacts, *, grace_seconds=ARTIFACT_GRACE_SECONDS) -> bool:
    mtimes = [float(item["mtime_epoch"]) for item in artifacts if item.get("mtime_epoch") is not None]
    if not mtimes or last_success_started_at is None:
        return True
    return max(mtimes) + grace_seconds < last_success_started_at.timestamp()


def _format_time(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def build_alert_text(event: str, alert: dict[str, Any], *, now: datetime) -> str:
    title = _EVENT_TITLES.get(event, _EVENT_TITLES["open"])
    display_name = str(alert.get("display_name") or "未命名任务")
    job_key = str(alert.get("job_key") or "")
    if event == "resolved":
        summary = "同步恢复"
    else:
        summary = _ALERT_KIND_LABELS.get(str(alert.get("alert_kind") or ""), "同步异常")

    lines = [title, f"{summary}：{display_name}"]
    if summary == "同步失败":
        error_kind = _normalized_error_kind(alert.get("error_kind"))
        lines.append(f"原因：{error_kind_label(error_kind)}({error_kind})")
    failures = alert.get("consecutive_failures")
    if isinstance(failures, int) and failures > 0:
        lines.append(f"连续失败 {failures} 次")
    last_success_at = _format_time(alert.get("last_success_at"))
    if last_success_at is not None:
        lines.append(f"上次成功：{last_success_at}")
    lines.append(f"查看任务：https://hydwang.xyz/sync/?job={quote(job_key, safe='')}")
    return "\n".join(lines)


class SyncAlertRepository:
    def __init__(
        self, conn: Any, *, now_fn: Callable[[], datetime], escalation_seconds: int = 21600
    ) -> None:
        self.conn = conn
        self.now_fn = now_fn
        self.escalation_seconds = escalation_seconds

    @staticmethod
    def _alert_from_row(row: Any) -> dict[str, Any]:
        if isinstance(row, dict):
            return dict(row)
        return {
            "id": row[0],
            "job_id": row[1],
            "run_id": row[2],
            "alert_kind": row[3],
            "first_seen_at": row[4],
            "last_notified_at": row[5],
            "notify_count": row[6],
            "payload_json": row[7] or {},
        }

    def claim_alert(self, job: dict[str, Any], run_id: int | None, alert_kind: str, payload: dict[str, Any]) -> int | None:
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sync_job_alerts (job_id, run_id, alert_kind, payload_json)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (job_id, alert_kind) WHERE state = 'open' DO NOTHING
                    RETURNING id
                    """,
                    (int(job["id"]), run_id, alert_kind, Jsonb(payload)),
                )
                row = cur.fetchone()
            self.conn.commit()
            return int(row[0]) if row else None
        except Exception:
            self.conn.rollback()
            raise

    def _lock_open_alert(self, cur: Any, alert_id: int) -> dict[str, Any] | None:
        cur.execute(
            """
            SELECT id, job_id, run_id, alert_kind, first_seen_at, last_notified_at, notify_count, payload_json
            FROM sync_job_alerts
            WHERE id = %s AND state = 'open'
            FOR UPDATE SKIP LOCKED
            """,
            (alert_id,),
        )
        row = cur.fetchone()
        return self._alert_from_row(row) if row else None

    @staticmethod
    def _sender_alert(alert: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        result = dict(alert)
        stored_payload = alert.get("payload_json")
        if isinstance(stored_payload, dict):
            result.update(stored_payload)
        if payload:
            result.update(payload)
        return result

    def deliver_due(self, alert_id: int, sender: Callable[[dict[str, Any]], bool]) -> bool:
        cutoff = self.now_fn() - timedelta(seconds=self.escalation_seconds)
        try:
            with self.conn.cursor() as cur:
                alert = self._lock_open_alert(cur, alert_id)
                if alert is None:
                    self.conn.rollback()
                    return False
                last_notified_at = alert.get("last_notified_at")
                if last_notified_at is not None and last_notified_at >= cutoff:
                    self.conn.rollback()
                    return False
                if not sender(self._sender_alert(alert)):
                    self.conn.rollback()
                    return False
                cur.execute(
                    """
                    UPDATE sync_job_alerts
                    SET last_notified_at = NOW(),
                        notify_count = notify_count + 1
                    WHERE id = %s
                      AND state = 'open'
                      AND (last_notified_at IS NULL OR last_notified_at < %s)
                    """,
                    (alert_id, cutoff),
                )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            return False

    def resolve_alert(self, alert_id: int, payload: dict[str, Any], sender: Callable[[dict[str, Any]], bool]) -> bool:
        try:
            with self.conn.cursor() as cur:
                alert = self._lock_open_alert(cur, alert_id)
                if alert is None or not sender(self._sender_alert(alert, payload)):
                    self.conn.rollback()
                    return False
                cur.execute(
                    """
                    UPDATE sync_job_alerts
                    SET state = 'resolved',
                        resolved_at = NOW(),
                        payload_json = %s
                    WHERE id = %s AND state = 'open'
                    """,
                    (Jsonb(payload), alert_id),
                )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            return False

    def load_job_states(self) -> list[dict[str, Any]]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        j.id, j.job_key, j.provider, j.display_name, j.enabled,
                        j.alert_enabled, j.alert_chat_id, j.freshness_sla_seconds, j.artifact_glob,
                        latest_run.id, latest_run.status, latest_run.started_at, latest_run.finished_at,
                        latest_run.error_kind, latest_run.error_message, latest_run.detail_json,
                        latest_success.id, latest_success.started_at, latest_success.finished_at, latest_success.detail_json,
                        COALESCE(
                            (
                                SELECT COUNT(*)
                                FROM sync_job_runs failed_run
                                WHERE failed_run.job_id = j.id
                                  AND failed_run.status IN ('failed', 'partial')
                                  AND failed_run.started_at > COALESCE(
                                      latest_success.finished_at,
                                      '-infinity'::timestamptz
                                  )
                            ),
                            0
                        ) AS consecutive_failures,
                        COALESCE(open_alerts.items, '[]'::jsonb) AS open_alerts
                    FROM sync_jobs j
                    LEFT JOIN LATERAL (
                        SELECT id, status, started_at, finished_at, error_kind, error_message, detail_json
                        FROM sync_job_runs
                        WHERE job_id = j.id
                        ORDER BY started_at DESC, id DESC
                        LIMIT 1
                    ) latest_run ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT id, started_at, finished_at, detail_json
                        FROM sync_job_runs
                        WHERE job_id = j.id AND status = 'success'
                        ORDER BY finished_at DESC NULLS LAST, id DESC
                        LIMIT 1
                    ) latest_success ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'id', open_alert.id,
                                'run_id', open_alert.run_id,
                                'alert_kind', open_alert.alert_kind,
                                'first_seen_at', open_alert.first_seen_at,
                                'last_notified_at', open_alert.last_notified_at,
                                'notify_count', open_alert.notify_count,
                                'payload_json', open_alert.payload_json
                            )
                            ORDER BY open_alert.id
                        ) AS items
                        FROM sync_job_alerts open_alert
                        WHERE open_alert.job_id = j.id AND open_alert.state = 'open'
                    ) open_alerts ON TRUE
                    ORDER BY j.id
                    """
                )
                rows = cur.fetchall()
            states = [
                {
                    "job": {
                        "id": row[0],
                        "job_key": row[1],
                        "provider": row[2],
                        "display_name": row[3],
                        "enabled": row[4],
                        "alert_enabled": row[5],
                        "alert_chat_id": row[6],
                        "freshness_sla_seconds": row[7],
                        "artifact_glob": row[8],
                    },
                    "latest_run": {
                        "id": row[9],
                        "status": row[10],
                        "started_at": row[11],
                        "finished_at": row[12],
                        "error_kind": row[13],
                        "error_message": row[14],
                        "detail_json": row[15] or {},
                    } if row[9] is not None else None,
                    "latest_success": {
                        "id": row[16],
                        "started_at": row[17],
                        "finished_at": row[18],
                        "detail_json": row[19] or {},
                    } if row[16] is not None else None,
                    "consecutive_failures": int(row[20]),
                    "open_alerts": list(row[21] or []),
                }
                for row in rows
            ]
            self.conn.commit()
            return states
        except Exception:
            self.conn.rollback()
            raise

    def cleanup_steps(self) -> int:
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM sync_job_steps s USING sync_job_runs r
                    WHERE s.run_id = r.id AND (
                      (r.status = 'success' AND r.finished_at < NOW() - INTERVAL '30 days') OR
                      (r.status <> 'success' AND r.finished_at < NOW() - INTERVAL '90 days')
                    )
                    """
                )
                deleted = int(cur.rowcount or 0)
            self.conn.commit()
            return deleted
        except Exception:
            self.conn.rollback()
            raise
