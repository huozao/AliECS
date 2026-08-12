from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


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
