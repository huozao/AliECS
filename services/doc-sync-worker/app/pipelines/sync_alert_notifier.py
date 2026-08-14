from __future__ import annotations

import base64
import glob
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from app.providers.feishu import FeishuBitableClient, credentials_for_profile

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
MIRROR_LAG_SECONDS = 900
MIRROR_RETRY_ALERT_ATTEMPTS = 3
MIRROR_JOB_KEY = "wecom.locator_mirror"

_EVENT_TITLES = {
    "open": "同步告警",
    "escalate": "同步告警升级",
    "resolved": "同步已恢复",
}
_ALERT_KIND_LABELS = {
    "failed": "同步失败",
    "stale": "同步延迟",
    "credential_expiring": "凭据告警",
    "artifact_stale": "产出物延迟",
    "mirror_lag": "定位档案镜像延迟",
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
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
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
        summary = _ALERT_KIND_LABELS.get(
            str(alert.get("alert_kind") or alert.get("status") or ""), "同步异常"
        )

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


def send_feishu_text(chat_id: str, text: str) -> bool:
    if not str(chat_id or "").strip():
        return False
    client = None
    try:
        profile = os.getenv("SYNC_ALERT_FEISHU_PROFILE", "COMPANY_A").strip() or "COMPANY_A"
        credentials = credentials_for_profile(profile)
        if not credentials:
            return False
        credential = credentials[0]
        client = FeishuBitableClient(
            app_id=credential.app_id,
            app_secret=credential.app_secret,
            api_base=credential.api_base,
        )
        client._request_json(
            "POST",
            "/im/v1/messages",
            headers=client._headers(),
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        return True
    except Exception as exc:
        print(f"[sync-alert] feishu send failed: {type(exc).__name__}")
        return False
    finally:
        if client is not None:
            try:
                client.session.close()
            except Exception:
                pass


def _positive_env_seconds(name: str, fallback: int) -> int:
    try:
        value = int(os.getenv(name, "").strip())
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _last_success_at(state: dict[str, Any]) -> datetime | None:
    latest_success = state.get("latest_success")
    if not isinstance(latest_success, dict):
        return None
    value = latest_success.get("finished_at") or latest_success.get("started_at")
    return value if isinstance(value, datetime) else None


def _live_artifacts(pattern: str) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for matched in glob.glob(pattern):
        path = Path(matched)
        try:
            if path.is_file():
                artifacts.append({"name": path.name, "mtime_epoch": path.stat().st_mtime})
        except OSError:
            continue
    return artifacts


def _sanitized_error_message(value: Any) -> str:
    return "[REDACTED]" if str(value or "").strip() else ""


def _alert_conditions(
    state: dict[str, Any], *, now: datetime, artifact_grace_seconds: int
) -> dict[str, dict[str, Any]]:
    job = state["job"]
    latest_run = state.get("latest_run") if isinstance(state.get("latest_run"), dict) else None
    latest_success = state.get("latest_success") if isinstance(state.get("latest_success"), dict) else None
    last_success_at = _last_success_at(state)
    base_payload = {
        "job_key": str(job.get("job_key") or ""),
        "display_name": str(job.get("display_name") or ""),
        "last_success_at": _format_time(last_success_at),
    }
    conditions: dict[str, dict[str, Any]] = {}

    if latest_run and latest_run.get("status") in {"failed", "partial"}:
        conditions["failed"] = {
            **base_payload,
            "status": str(latest_run.get("status")),
            "error_kind": _normalized_error_kind(latest_run.get("error_kind")),
            "error_message": _sanitized_error_message(latest_run.get("error_message")),
            "consecutive_failures": int(state.get("consecutive_failures") or 0),
        }

    freshness_sla = job.get("freshness_sla_seconds")
    if isinstance(freshness_sla, int) and not isinstance(freshness_sla, bool) and freshness_sla > 0:
        stale = last_success_at is None or (
            _as_utc(now) - _as_utc(last_success_at)
        ).total_seconds() > freshness_sla
        if stale:
            conditions["stale"] = {
                **base_payload,
                "status": "stale",
                "freshness_sla_seconds": freshness_sla,
            }

    artifact_pattern = str(job.get("artifact_glob") or "").strip()
    if artifact_pattern:
        artifacts = _live_artifacts(artifact_pattern)
        success_started_at = latest_success.get("started_at") if latest_success else None
        if artifact_is_stale(
            success_started_at, artifacts, grace_seconds=artifact_grace_seconds
        ):
            conditions["artifact_stale"] = {
                **base_payload,
                "status": "artifact_stale",
                "artifacts": [
                    {
                        "name": Path(str(item.get("name") or "")).name,
                        "mtime_epoch": item.get("mtime_epoch"),
                    }
                    for item in artifacts
                ],
            }

    if str(job.get("job_key") or "") == "chanjet.full":
        token_path = os.getenv("CHANJET_OPEN_TOKEN_FILE", "").strip()
        if token_path:
            token = credential_status(token_path, now=now)
            if not token["ok"]:
                conditions["credential_expiring"] = {
                    **base_payload,
                    "status": "credential_expiring",
                    "configured": bool(token["configured"]),
                    "expired": bool(token["expired"]),
                    "expires_at": _format_time(token["expires_at"]),
                    "remaining_hours": token["remaining_hours"],
                    "message": token["message"],
                }

    if str(job.get("job_key") or "") == MIRROR_JOB_KEY:
        health = state.get("mirror_health") if isinstance(state.get("mirror_health"), dict) else {}
        pending_count = int(health.get("pending_count") or 0)
        max_attempt_count = int(health.get("max_attempt_count") or 0)
        failed_count = int(health.get("failed_count") or 0)
        oldest_pending_at = health.get("oldest_pending_at")
        pending_too_long = bool(
            pending_count
            and isinstance(oldest_pending_at, datetime)
            and (_as_utc(now) - _as_utc(oldest_pending_at)).total_seconds() > MIRROR_LAG_SECONDS
        )
        if failed_count or max_attempt_count >= MIRROR_RETRY_ALERT_ATTEMPTS or pending_too_long:
            conditions["mirror_lag"] = {
                **base_payload,
                "status": "mirror_lag",
                "pending_count": pending_count,
                "failed_count": failed_count,
                "max_attempt_count": max_attempt_count,
            }

    return conditions


def _safe_send(
    sender: Callable[[str, str], bool], chat_id: str, event: str, alert: dict[str, Any], now: datetime
) -> bool:
    if not chat_id:
        return False
    try:
        return bool(sender(chat_id, build_alert_text(event, alert, now=now)))
    except Exception as exc:
        print(f"[sync-alert] sender failed: {type(exc).__name__}")
        return False


def _alert_has_recovered(
    alert: dict[str, Any], conditions: dict[str, dict[str, Any]], state: dict[str, Any]
) -> bool:
    alert_kind = str(alert.get("alert_kind") or "")
    if alert_kind != "failed":
        return alert_kind not in conditions
    alert_run_id = alert.get("run_id")
    latest_success = state.get("latest_success")
    if alert_run_id is None or not isinstance(latest_success, dict):
        return False
    success_run_id = latest_success.get("id")
    return (
        type(alert_run_id) is int
        and type(success_run_id) is int
        and success_run_id > alert_run_id
    )


def run_notifier_once(
    *, repository=None, sender=None, now=None
) -> dict[str, int]:
    current = _as_utc(now or datetime.now(timezone.utc))
    escalation_seconds = _positive_env_seconds("SYNC_ALERT_ESCALATION_SECONDS", 21600)
    artifact_grace_seconds = _positive_env_seconds("SYNC_ARTIFACT_GRACE_SECONDS", 300)
    owned_connection = None
    if repository is None:
        from app.storage.postgres import connect

        owned_connection = connect()
        repository = SyncAlertRepository(
            owned_connection, now_fn=lambda: current, escalation_seconds=escalation_seconds
        )
    else:
        repository.escalation_seconds = escalation_seconds
    send = sender or send_feishu_text
    result = {
        "checked": 0,
        "opened": 0,
        "resolved": 0,
        "notified": 0,
        "escalated": 0,
        "cleaned": 0,
    }

    try:
        try:
            repository.ensure_chanjet_defaults()
        except Exception as exc:  # noqa: BLE001 - 默认值写入不能阻断已有告警轮询
            print(f"[sync-alert] chanjet defaults failed: {type(exc).__name__}")
        states = repository.load_job_states()
        mirror_state = next(
            (
                state
                for state in states
                if str((state.get("job") or {}).get("job_key") or "") == MIRROR_JOB_KEY
            ),
            None,
        )
        if mirror_state is not None:
            try:
                mirror_state["mirror_health"] = repository.load_locator_mirror_health()
            except Exception as exc:  # noqa: BLE001 - mirror health must not block other alerts.
                print(f"[sync-alert] locator mirror health failed: {type(exc).__name__}")
        result["checked"] = len(states)
        contexts: list[dict[str, Any]] = []
        for state in states:
            job = state["job"]
            if not job.get("enabled", True) or not job.get("alert_enabled", True):
                continue
            chat_id = str(job.get("alert_chat_id") or os.getenv("SYNC_ALERT_CHAT_ID", "")).strip()
            conditions = _alert_conditions(
                state, now=current, artifact_grace_seconds=artifact_grace_seconds
            )
            open_alerts = [
                alert for alert in state.get("open_alerts", []) if isinstance(alert, dict)
            ]
            open_by_kind = {
                str(alert.get("alert_kind")): alert for alert in open_alerts
            }
            contexts.append({
                "state": state,
                "job": job,
                "chat_id": chat_id,
                "conditions": conditions,
                "open_by_kind": open_by_kind,
                "resolved_kinds": set(),
            })

        for context in contexts:
            state = context["state"]
            job = context["job"]
            chat_id = context["chat_id"]
            conditions = context["conditions"]
            open_by_kind = context["open_by_kind"]
            for alert_kind, alert in open_by_kind.items():
                if not _alert_has_recovered(alert, conditions, state):
                    continue
                recovery_payload = {
                    "job_key": str(job.get("job_key") or ""),
                    "display_name": str(job.get("display_name") or ""),
                    "status": "recovered",
                    "last_success_at": _format_time(_last_success_at(state)),
                }
                if repository.resolve_alert(
                    int(alert["id"]),
                    recovery_payload,
                    lambda delivered, cid=chat_id: _safe_send(
                        send, cid, "resolved", delivered, current
                    ),
                ):
                    result["resolved"] += 1
                    context["resolved_kinds"].add(alert_kind)

        due_alerts: list[tuple[int, bool, str, dict[str, Any]]] = []
        for context in contexts:
            state = context["state"]
            job = context["job"]
            chat_id = context["chat_id"]
            conditions = context["conditions"]
            open_by_kind = context["open_by_kind"]
            resolved_kinds = context["resolved_kinds"]
            latest_run = state.get("latest_run")
            run_id = int(latest_run["id"]) if isinstance(latest_run, dict) and latest_run.get("id") is not None else None
            for alert_kind, payload in conditions.items():
                existing = open_by_kind.get(alert_kind)
                if existing is not None and alert_kind not in resolved_kinds:
                    due_alerts.append((
                        int(existing["id"]),
                        int(existing.get("notify_count") or 0) > 0,
                        chat_id,
                        payload,
                    ))
                    continue
                alert_id = repository.claim_alert(
                    job, run_id if alert_kind == "failed" else None, alert_kind, payload
                )
                if alert_id is not None:
                    result["opened"] += 1
                    due_alerts.append((int(alert_id), False, chat_id, payload))

        for alert_id, is_escalation, chat_id, payload in due_alerts:
            event = "escalate" if is_escalation else "open"
            if repository.deliver_due(
                alert_id,
                lambda delivered, cid=chat_id, alert_event=event: _safe_send(
                    send, cid, alert_event, delivered, current
                ),
                payload=payload,
            ):
                result["notified"] += 1
                if is_escalation:
                    result["escalated"] += 1

        result["cleaned"] = int(repository.cleanup_steps() or 0)
        return result
    finally:
        if owned_connection is not None:
            owned_connection.close()


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

    def ensure_chanjet_defaults(self) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sync_jobs
                    SET freshness_sla_seconds = COALESCE(freshness_sla_seconds, %s),
                        artifact_glob = COALESCE(artifact_glob, %s),
                        updated_at = NOW()
                    WHERE job_key = %s
                    """,
                    (172800, "/app/tplus-output/excel/*.xlsx", "chanjet.full"),
                )
                cur.execute(
                    """
                    INSERT INTO sync_jobs(job_key, kind, provider, display_name, enabled, alert_enabled, updated_at)
                    VALUES (%s, 'mirror', 'wecom', %s, TRUE, TRUE, NOW())
                    ON CONFLICT(job_key) DO UPDATE SET
                        kind = EXCLUDED.kind,
                        provider = EXCLUDED.provider,
                        display_name = EXCLUDED.display_name,
                        enabled = TRUE,
                        updated_at = NOW()
                    """,
                    (MIRROR_JOB_KEY, "企微文档定位档案镜像"),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def load_locator_mirror_health(self) -> dict[str, Any]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status IN ('pending', 'running')),
                        MIN(created_at) FILTER (WHERE status IN ('pending', 'running')),
                        COALESCE(MAX(attempt_count) FILTER (WHERE status IN ('pending', 'running')), 0),
                        COUNT(*) FILTER (WHERE status = 'failed')
                    FROM document_locator_mirror_jobs
                    """
                )
                rows = cur.fetchall()
            row = rows[0] if rows else (0, None, 0, 0)
            self.conn.commit()
            return {
                "pending_count": int(row[0] or 0),
                "oldest_pending_at": row[1],
                "max_attempt_count": int(row[2] or 0),
                "failed_count": int(row[3] or 0),
            }
        except Exception:
            self.conn.rollback()
            raise

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

    def deliver_due(
        self,
        alert_id: int,
        sender: Callable[[dict[str, Any]], bool],
        payload: dict[str, Any] | None = None,
    ) -> bool:
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
                current_payload = payload if payload is not None else alert.get("payload_json")
                if not isinstance(current_payload, dict):
                    current_payload = {}
                if not sender(self._sender_alert(alert, current_payload)):
                    self.conn.rollback()
                    return False
                cur.execute(
                    """
                    UPDATE sync_job_alerts
                    SET payload_json = %s,
                        last_notified_at = NOW(),
                        notify_count = notify_count + 1
                    WHERE id = %s
                      AND state = 'open'
                      AND (last_notified_at IS NULL OR last_notified_at < %s)
                    """,
                    (Jsonb(current_payload), alert_id, cutoff),
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
