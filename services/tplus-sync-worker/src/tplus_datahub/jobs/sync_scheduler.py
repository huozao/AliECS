from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


BEIJING = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ScheduleDecision:
    due: datetime
    run_full: bool
    wait_seconds: int


def normalize_mode(raw: str | None) -> str:
    return raw if raw in {"legacy", "shadow", "active"} else "legacy"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _next_due(now: datetime, last_full: datetime | None, interval_seconds: int, anchor_time: str) -> datetime:
    now = _as_utc(now)
    interval = max(int(interval_seconds), 60)
    if last_full is None:
        return now
    last_full = _as_utc(last_full)
    anchor_text = str(anchor_time or "").strip()
    if not anchor_text:
        return last_full + timedelta(seconds=interval)
    hour, minute = (int(part) for part in anchor_text.split(":"))
    anchor_local = last_full.astimezone(BEIJING).replace(hour=hour, minute=minute, second=0, microsecond=0)
    anchor = anchor_local.astimezone(timezone.utc)
    if anchor > last_full:
        steps = int((anchor - last_full).total_seconds() // interval) + 1
        anchor -= timedelta(seconds=steps * interval)
    due = anchor + timedelta(seconds=interval)
    while due <= last_full:
        due += timedelta(seconds=interval)
    return due


def decide(now, last_full, enabled, interval_seconds, anchor_time) -> ScheduleDecision:
    now = _as_utc(now)
    due = _next_due(now, last_full, interval_seconds, anchor_time)
    run_full = bool(enabled and due <= now)
    return ScheduleDecision(due, run_full, max(int((due - now).total_seconds()), 0))


def target_moved_earlier(planned_due: datetime, candidate_due: datetime, tolerance_seconds: int = 30) -> bool:
    return _as_utc(candidate_due) < _as_utc(planned_due) - timedelta(seconds=tolerance_seconds)


def _decision_payload(decision: ScheduleDecision) -> dict[str, object]:
    return {
        "due": _as_utc(decision.due).isoformat(),
        "run_full": bool(decision.run_full),
        "wait_seconds": int(decision.wait_seconds),
    }


def shadow_payload(*, sampled_at: datetime, legacy: ScheduleDecision, candidate: ScheduleDecision) -> dict[str, object]:
    legacy_due = _as_utc(legacy.due)
    candidate_due = _as_utc(candidate.due)
    return {
        "mode": "shadow",
        "sampled_at": _as_utc(sampled_at).isoformat(),
        "legacy": _decision_payload(legacy),
        "candidate": _decision_payload(candidate),
        "decision_match": (
            legacy_due == candidate_due
            and bool(legacy.run_full) == bool(candidate.run_full)
            and int(legacy.wait_seconds) == int(candidate.wait_seconds)
        ),
        "due_delta_seconds": float((candidate_due - legacy_due).total_seconds()),
    }
