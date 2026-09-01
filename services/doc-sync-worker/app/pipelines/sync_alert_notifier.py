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

from app import notify_client

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
# 「多久没确认新鲜就该有人管」永远要比调度周期宽出一段，否则每天在「上次完成 +周期」
# 到「本轮完成」之间必然出现一个 stale 窗口——2026-09-01 实测每晚 16 条误告警 + 16 条
# 恢复，页面上却是全成功。宽限取绝对值，与周期无关。
STALE_GRACE_SECONDS = 7200
DEFAULT_INTERVAL_SECONDS = 86400
# 作业的 provider → integration_sync_config 里的调度 provider。
SCHEDULE_PROVIDER_BY_JOB_PROVIDER = {
    "wecom": "doc_sync",
    "feishu": "doc_sync",
    "chanjet": "chanjet",
}
SYNC_PAGE_URL = "https://hydwang.xyz/sync/"
# 一条聚合消息里每个文档最多点名几张表，超出只报数。
MAX_NAMED_SHEETS_PER_DOCUMENT = 3
MAX_LISTED_DOCUMENTS = 5
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


def _document_name(item: dict[str, Any]) -> str:
    """告警条目所属的文档。作业挂在表级来源上，来源行带 document_name。

    非文档类作业（chanjet.full、locator_mirror、parent_match）没有来源行，
    退回 display_name 的「文档 / 表」前半段，再退回 display_name 本身。
    """
    document = str(item.get("document_name") or "").strip()
    if document:
        return document
    display_name = str(item.get("display_name") or "").strip()
    if " / " in display_name:
        return display_name.split(" / ", 1)[0].strip()
    return display_name or "未命名任务"


def _sheet_name(item: dict[str, Any]) -> str:
    sheet = str(item.get("sheet_name") or "").strip()
    if sheet:
        return sheet
    display_name = str(item.get("display_name") or "").strip()
    if " / " in display_name:
        return display_name.split(" / ", 1)[1].strip()
    return display_name or "未命名任务"


def _kind_of(item: dict[str, Any]) -> str:
    return str(item.get("alert_kind") or item.get("status") or "")


def _earliest_last_success(items: list[dict[str, Any]]) -> str | None:
    stamps = [_format_time(item.get("last_success_at")) for item in items]
    present = sorted(stamp for stamp in stamps if stamp)
    return present[0] if present else None


def _document_lines(items: list[dict[str, Any]]) -> list[str]:
    """把一组条目按文档折成每文档一行：文档名 + 张数 + 点名前几张表。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(_document_name(item), []).append(item)
    lines: list[str] = []
    for document, members in list(grouped.items())[:MAX_LISTED_DOCUMENTS]:
        sheets = [_sheet_name(member) for member in members]
        named = "、".join(sheets[:MAX_NAMED_SHEETS_PER_DOCUMENT])
        if len(sheets) > MAX_NAMED_SHEETS_PER_DOCUMENT:
            named = f"{named} 等 {len(sheets)} 张"
        lines.append(f"· {document} {len(sheets)} 张：{named}")
    hidden = len(grouped) - MAX_LISTED_DOCUMENTS
    if hidden > 0:
        lines.append(f"· 另有 {hidden} 个文档，详见页面")
    return lines


def build_batch_text(event: str, items: list[dict[str, Any]], *, now: datetime) -> str:
    """把一次轮询里同一收件人、同一事件方向的多条告警折成一条消息。

    分组只做在发送层：库里仍然一个作业一行告警，notify_count、6 小时升级节流、
    解除判定全都不变。一个文档下几十张子表同时延迟时，用户收到的是一条而不是几十条。
    """
    if len(items) == 1:
        return build_alert_text(event, items[0], now=now)

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_kind.setdefault(_kind_of(item), []).append(item)
    # 失败永远排在延迟前面：一条消息里最需要先看到的是真故障。
    ordered_kinds = sorted(by_kind, key=lambda kind: (kind != "failed", kind))

    resolved = event == "resolved"
    documents = len({_document_name(item) for item in items})
    single_kind = len(ordered_kinds) == 1
    if resolved:
        # 恢复消息不复述告警类型：已经好了，用户要的是「哪些、多少」。
        headline = f"{len(items)} 张 / {documents} 个文档"
    elif single_kind:
        headline = f"{_ALERT_KIND_LABELS.get(ordered_kinds[0], '同步异常')} {len(items)} 张 / {documents} 个文档"
    else:
        headline = "、".join(
            f"{_ALERT_KIND_LABELS.get(kind, '同步异常')} {len(by_kind[kind])} 张"
            for kind in ordered_kinds
        )
    title = _EVENT_TITLES.get(event, _EVENT_TITLES["open"])
    lines = [f"{title} · {headline}"]

    for kind in ordered_kinds:
        members = by_kind[kind]
        label = _ALERT_KIND_LABELS.get(kind, "同步异常")
        if not single_kind:
            # 只有一类时标题已经说完了，再来一行段头就是复述。
            lines.append(f"【{label}】{len(members)} 张 / {len({_document_name(m) for m in members})} 个文档")
        if kind == "failed" and not resolved:
            for member in members[:MAX_LISTED_DOCUMENTS]:
                error_kind = _normalized_error_kind(member.get("error_kind"))
                failures = member.get("consecutive_failures")
                suffix = f"，连续 {failures} 次" if isinstance(failures, int) and failures > 0 else ""
                lines.append(
                    f"· {member.get('display_name') or '未命名任务'}："
                    f"{error_kind_label(error_kind)}({error_kind}){suffix}"
                )
            if len(members) > MAX_LISTED_DOCUMENTS:
                lines.append(f"· 另有 {len(members) - MAX_LISTED_DOCUMENTS} 张，详见页面")
        else:
            lines.extend(_document_lines(members))

    earliest = _earliest_last_success(items)
    if earliest is not None:
        lines.append(f"{'最近成功' if resolved else '最早上次成功'}：{earliest}")
    lines.append(f"查看：{SYNC_PAGE_URL}")
    return "\n".join(lines)


def send_feishu_text(chat_id: str, text: str, *, conn: Any = None, commit: bool = True) -> bool:
    """交给统一消息中枢：只往 notify_outbox 写一行，投递由 backend-api 负责。

    收敛前这里自己取 tenant token、自己调 im/v1/messages，与另外三处各写一套。
    ``chat_id`` 留在签名里是为了不动调用点；**收件人现在由 notify_routes 决定**，
    这个参数只作为 dedup_key 的一部分参与去重。

    返回 False 的含义随之改变：以前是「飞书没收到」，现在是「没能写进 outbox」。
    真正的投递结果要查 notify_deliveries。
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    payload = notify_client.build_payload(
        source="doc-sync",
        event="sync_alert",
        level="warn",
        title=lines[0].strip(),
        # 告警正文是排好版的多行文本，交给 markdown 会被重排。
        text_segments=["\n".join(lines[1:])] if len(lines) > 1 else [],
    )
    for segment in payload["segments"]:
        if segment.get("kind") == "text":
            segment["preformatted"] = True
    # 传 conn 时这行 outbox 与告警状态的更新落在同一个事务里：要么都成，要么都不成，
    # 不会出现「标了已通知但消息没写进去」这种失败态与未执行态长得一样的情况。
    return notify_client.enqueue(payload, conn=conn, commit=commit)


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


def _schedule_interval_seconds(state: dict[str, Any]) -> int:
    """该作业实际的调度周期；读不到就按一天算（比它小会误报，比它大只是晚报）。"""
    value = state.get("schedule_interval_seconds")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_INTERVAL_SECONDS


def _alert_conditions(
    state: dict[str, Any],
    *,
    now: datetime,
    artifact_grace_seconds: int,
    stale_grace_seconds: int = STALE_GRACE_SECONDS,
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
        # ⚠️ 阈值不能直接用配的 SLA：它可能被配成正好等于调度周期（2026-08-30 补的
        # 「控制面 24h」档就是），那样余量为零，起跑时间只要比昨天晚一分钟就报。
        # 取「调度周期 + 宽限」兜底，等价于「下一次应有运行时间之后还没来才算延迟」。
        threshold = max(freshness_sla, _schedule_interval_seconds(state) + stale_grace_seconds)
        stale = last_success_at is None or (
            _as_utc(now) - _as_utc(last_success_at)
        ).total_seconds() > threshold
        if stale:
            conditions["stale"] = {
                **base_payload,
                "status": "stale",
                "freshness_sla_seconds": freshness_sla,
                "stale_threshold_seconds": threshold,
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

    for kind, condition_payload in conditions.items():
        # 分组和文案都按 alert_kind 走：failed 的 payload 里 status 可能是 partial，
        # 拿 status 当类型会把同一类告警拆成两组。
        condition_payload.setdefault("alert_kind", kind)
    return conditions


def _dispatch_batches(
    outgoing: list[dict[str, Any]],
    sender: Callable[[str, str], bool],
    *,
    now: datetime,
) -> bool:
    """把收集到的告警按「收件人 + 方向」折成聚合消息发出去。

    方向只分两种：告警（open/escalate 合并，升级本质是同一批告警的再通知）和恢复。
    一条消息里再按 alert_kind、文档二级分组，见 :func:`build_batch_text`。
    只要有一条发不出去就返回 False，调用方会把这一轮的状态整体回滚。
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for message in outgoing:
        direction = "resolved" if message["event"] == "resolved" else "alert"
        grouped.setdefault((message["chat_id"], direction), []).append(message)

    delivered = True
    for (chat_id, direction), members in grouped.items():
        if direction == "resolved":
            event = "resolved"
        else:
            event = "escalate" if all(m["event"] == "escalate" for m in members) else "open"
        text = build_batch_text(event, [m["item"] for m in members], now=now)
        try:
            if not sender(chat_id, text):
                delivered = False
        except Exception as exc:  # noqa: BLE001 - 发送异常不得中断本轮其余分组
            print(f"[sync-alert] sender failed: {type(exc).__name__}")
            delivered = False
    return delivered


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
    stale_grace_seconds = _positive_env_seconds("SYNC_STALE_GRACE_SECONDS", STALE_GRACE_SECONDS)
    owned_connection = None
    if repository is None:
        from app.storage.postgres import connect

        owned_connection = connect()
        repository = SyncAlertRepository(
            owned_connection, now_fn=lambda: current, escalation_seconds=escalation_seconds
        )
    else:
        repository.escalation_seconds = escalation_seconds
    if sender is not None:
        send = sender
    else:
        # 与告警状态共用同一个连接和事务；拿不到连接（注入的假仓库）就退回自管自提交。
        shared_conn = getattr(repository, "conn", None)
        send = lambda cid, text: send_feishu_text(  # noqa: E731
            cid, text, conn=shared_conn, commit=shared_conn is None
        )
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
        try:
            schedule_intervals = repository.load_schedule_intervals()
        except Exception as exc:  # noqa: BLE001 - 读不到周期就退回默认，不阻断告警
            print(f"[sync-alert] schedule intervals failed: {type(exc).__name__}")
            schedule_intervals = {}
        for state in states:
            provider = str((state.get("job") or {}).get("provider") or "")
            schedule_provider = SCHEDULE_PROVIDER_BY_JOB_PROVIDER.get(provider, "")
            state["schedule_interval_seconds"] = schedule_intervals.get(
                schedule_provider, DEFAULT_INTERVAL_SECONDS
            )
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
                state,
                now=current,
                artifact_grace_seconds=artifact_grace_seconds,
                stale_grace_seconds=stale_grace_seconds,
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

        # 发送改成两段：先把该发的收集起来，全部处理完再按「收件人 + 方向」折成
        # 聚合消息发出去。库里仍然一个作业一行告警，notify_count / 6 小时升级节流 /
        # 解除判定都不变，只是用户不再一次收到几十条。
        outgoing: list[dict[str, Any]] = []

        def _collector(event: str, chat_id: str, job: dict[str, Any]):
            def _append(delivered: dict[str, Any]) -> bool:
                if not chat_id:
                    return False
                outgoing.append({
                    "event": event,
                    "chat_id": chat_id,
                    "item": {
                        **delivered,
                        "document_name": job.get("document_name"),
                        "sheet_name": job.get("sheet_name"),
                    },
                })
                return True

            return _append

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
                    _collector("resolved", chat_id, job),
                    defer_commit=True,
                ):
                    result["resolved"] += 1
                    context["resolved_kinds"].add(alert_kind)

        due_alerts: list[tuple[int, bool, str, dict[str, Any], dict[str, Any]]] = []
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
                        job,
                    ))
                    continue
                alert_id = repository.claim_alert(
                    job, run_id if alert_kind == "failed" else None, alert_kind, payload
                )
                if alert_id is not None:
                    result["opened"] += 1
                    due_alerts.append((int(alert_id), False, chat_id, payload, job))

        for alert_id, is_escalation, chat_id, payload, job in due_alerts:
            event = "escalate" if is_escalation else "open"
            if repository.deliver_due(
                alert_id,
                _collector(event, chat_id, job),
                payload=payload,
                defer_commit=True,
            ):
                result["notified"] += 1
                if is_escalation:
                    result["escalated"] += 1

        if outgoing:
            if _dispatch_batches(outgoing, send, now=current):
                repository.commit()
            else:
                # 一条都没发出去就把这一轮的状态全部退回：宁可下一轮重发，
                # 也不要留下「标了已通知、消息却没写进 outbox」的静默丢失。
                repository.rollback()
                print("[sync-alert] batch dispatch failed, rolled back this round")
                result["resolved"] = 0
                result["notified"] = 0
                result["escalated"] = 0

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

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def load_schedule_intervals(self) -> dict[str, int]:
        """读各 provider 的调度周期，供 stale 阈值兜底用。

        读不到就返回空 dict，判据那边会退回一天——宁可晚报也不误报。
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT provider, interval_seconds FROM integration_sync_config")
                rows = cur.fetchall()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            return {}
        intervals: dict[str, int] = {}
        for row in rows or []:
            try:
                provider = str(row[0])
                seconds = int(row[1])
            except (IndexError, TypeError, ValueError):
                continue
            if seconds > 0:
                intervals[provider] = seconds
        return intervals

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
        *,
        defer_commit: bool = False,
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
            if not defer_commit:
                self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            return False

    def resolve_alert(
        self,
        alert_id: int,
        payload: dict[str, Any],
        sender: Callable[[dict[str, Any]], bool],
        *,
        defer_commit: bool = False,
    ) -> bool:
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
            if not defer_commit:
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
                        COALESCE(open_alerts.items, '[]'::jsonb) AS open_alerts,
                        source.document_name, source.sheet_name
                    FROM sync_jobs j
                    -- 聚合通知按文档分组，文档名在表级来源行上（与 backend-api
                    -- app/sync_read.py 的资产视图同一个字段）。
                    LEFT JOIN external_sources source ON source.id = j.source_id
                    LEFT JOIN LATERAL (
                        SELECT id, status, started_at, finished_at, error_kind, error_message, detail_json
                        FROM sync_job_runs
                        WHERE job_id = j.id
                        ORDER BY started_at DESC, id DESC
                        LIMIT 1
                    ) latest_run ON TRUE
                    -- 新鲜度看的是「最近一次确认数据是新的」，不是「最近一次真的拉了数据」。
                    -- 与 backend-api app/sync_read.py 的 `verified` LATERAL 同一判据：
                    -- 企微 modify_time 未变会整簿跳过，跳过同样确认了内容没变；只认
                    -- success 的话，配上 SLA 之后那几十张长期无改动的表会集体变 stale。
                    LEFT JOIN LATERAL (
                        SELECT id, started_at, finished_at, detail_json
                        FROM sync_job_runs
                        WHERE job_id = j.id AND status IN ('success', 'skipped')
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
                        "document_name": row[22] if len(row) > 22 else None,
                        "sheet_name": row[23] if len(row) > 23 else None,
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
