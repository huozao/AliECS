"""全设备版本看板：采集上报、上游对比、看板查询、周报推送。"""

from __future__ import annotations

import hmac
import json
import os
import re
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.core import _conn, require_admin
from app.notify import dispatch
from app.notify.models import Notification

_V_PREFIX = re.compile(r"^(refs/tags/)?v", re.I)
_SUFFIX = re.compile(r"-(alpine|bookworm|slim|debian|distroless).*$", re.I)


def normalize_version(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = _V_PREFIX.sub("", s)
    s = _SUFFIX.sub("", s)
    return s.strip()


def _seg_cmp(a: str, b: str) -> int:
    if a.isdigit() and b.isdigit():
        ai, bi = int(a), int(b)
        return (ai > bi) - (ai < bi)
    return (a > b) - (a < b)


def compare_versions(current: str | None, latest: str | None) -> int:
    cur = normalize_version(current)
    lat = normalize_version(latest)
    if not cur or not lat:
        return 1  # 不可比时保守视为"不落后"，避免误报
    cs, ls = cur.split("."), lat.split(".")
    for i in range(max(len(cs), len(ls))):
        a = cs[i] if i < len(cs) else "0"
        b = ls[i] if i < len(ls) else "0"
        c = _seg_cmp(a, b)
        if c != 0:
            return c
    return 0


def match_component(image: str, device: str, components: list[dict]) -> dict | None:
    base = image.split("@")[0].split(":")[0]  # 去掉 tag/digest
    for comp in components:
        images = comp.get("match_images") or []
        if image in images or base in images:
            devices = comp.get("devices")
            if devices is None or device in devices:
                return comp
    return None


def classify_component(*, family: str, upstream_source: str,
                       current: str | None, latest: str | None,
                       version_pattern: str | None) -> str:
    if family == "own":
        return "own"
    if upstream_source == "none":
        return "pinned"
    if not current:
        return "stale"
    if not latest:
        return "pinned"
    return "behind" if compare_versions(current, latest) < 0 else "current"


router = APIRouter()


class ContainerReport(BaseModel):
    image: str = Field(min_length=1, max_length=300)
    tag: str | None = Field(default=None, max_length=200)
    digest: str | None = Field(default=None, max_length=200)


class VersionReport(BaseModel):
    device: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    containers: list[ContainerReport] = Field(default_factory=list, max_length=200)
    apt: dict[str, int] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


def _require_report_token(x_backup_report_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("BACKUP_REPORT_TOKEN", "").strip()
    supplied = (x_backup_report_token or "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="invalid report token")


@router.post("/v1/internal/versions/report")
def report_versions(body: VersionReport, _: None = Depends(_require_report_token)) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM version_reports WHERE device = %s", (body.device,))
                for c in body.containers:
                    cur.execute(
                        "INSERT INTO version_reports(device, image, tag, digest, extra_json) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (body.device, c.image, c.tag, c.digest, Jsonb({})),
                    )
                # apt 汇总 + extra（openclaw 版本等）作为一条 apt-summary 记录
                cur.execute(
                    "INSERT INTO version_reports(device, image, tag, digest, extra_json) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (body.device, "apt-summary", None, None,
                     Jsonb({**body.extra, "apt": body.apt})),
                )
            conn.commit()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"version report write failed: {type(exc).__name__}") from exc
    return {"ok": True, "device": body.device, "count": len(body.containers)}


_UA = {"User-Agent": "aliecs-version-inventory/1.0"}


def fetch_github_latest(ref: str, opener=urllib.request.urlopen) -> tuple[str | None, str | None]:
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{ref}/releases/latest", headers=_UA)
        with opener(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data.get("tag_name"), data.get("html_url")
    except Exception:
        return None, None


def fetch_dockerhub_latest(ref: str, pattern: str | None,
                           opener=urllib.request.urlopen) -> tuple[str | None, str | None]:
    try:
        req = urllib.request.Request(
            f"https://hub.docker.com/v2/repositories/{ref}/tags?page_size=100&ordering=last_updated",
            headers=_UA)
        with opener(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        rx = re.compile(pattern) if pattern else None
        best = None
        for t in data.get("results", []):
            name = t.get("name", "")
            if name == "latest" or (rx and not rx.search(name)):
                continue
            if not re.match(r"^[0-9]", normalize_version(name)):
                continue
            if best is None or compare_versions(name, best) > 0:
                best = name
        url = f"https://hub.docker.com/_/{ref.split('/')[-1]}?tab=tags"
        return best, (url if best else None)
    except Exception:
        return None, None


@router.post("/v1/internal/versions/refresh-upstream")
def refresh_upstream(_: None = Depends(_require_report_token)) -> dict[str, Any]:
    checked = 0
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT component_key, upstream_source, upstream_ref, version_pattern "
                "FROM version_components WHERE active AND upstream_source <> 'none'"
            )
            rows = cur.fetchall()
        for key, source, ref, pattern in rows:
            latest, url, status, err = None, None, "ok", None
            try:
                if source == "github-release":
                    latest, url = fetch_github_latest(ref)
                elif source == "dockerhub":
                    latest, url = fetch_dockerhub_latest(ref, pattern)
                if latest is None:
                    status, err = "error", "no upstream version resolved"
            except Exception as exc:
                status, err = "error", type(exc).__name__
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO version_upstream_state(component_key, latest_version, release_url, "
                        "checked_at, check_status, check_error) VALUES (%s, %s, %s, NOW(), %s, %s) "
                        "ON CONFLICT (component_key) DO UPDATE SET latest_version=EXCLUDED.latest_version, "
                        "release_url=EXCLUDED.release_url, checked_at=NOW(), "
                        "check_status=EXCLUDED.check_status, check_error=EXCLUDED.check_error",
                        (key, normalize_version(latest) if latest else None, url, status, err),
                    )
                # 每组件独立事务：成功即提交，失败不影响其他组件已提交的结果
                conn.commit()
            except Exception:
                # 单组件 UPSERT 失败不应中断整个循环，回滚本组件后跳过继续处理其余的
                conn.rollback()
                continue
            checked += 1
    return {"ok": True, "checked": checked}


def build_inventory(reports: list[dict], components: list[dict],
                    upstream: dict[str, dict]) -> dict[str, Any]:
    # own 家族跨设备 tag 收集
    own_tags: dict[str, set] = {}
    for r in reports:
        comp = match_component(r["image"], r["device"], components)
        if comp and comp.get("family") == "own":
            own_tags.setdefault(comp["component_key"], set()).add(r.get("tag"))

    # 每设备 apt-summary 行的 extra（承载 sha256 锁定镜像的真实版本，key == component_key）
    device_extra: dict[str, dict] = {}
    for r in reports:
        if r["image"] == "apt-summary":
            device_extra[r["device"]] = r.get("extra") or {}

    devices: dict[str, list] = {}
    drift_devices: list[dict[str, Any]] = []
    drift_counts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0, "STALE": 0}
    summary = {"behind": 0, "current": 0, "pinned": 0, "unregistered": 0,
               "own": 0, "own-mismatch": 0, "stale": 0}
    for r in reports:
        dev = r["device"]
        comp = match_component(r["image"], dev, components)
        if r["image"] == "apt-summary":
            extra = r.get("extra") or {}
            apt = extra.get("apt", {})
            entry = {"key": "apt-summary", "name": "APT 可升级", "current":
                     f"可升级 {apt.get('upgradable', 0)}（security {apt.get('security', 0)}）",
                     "latest": None, "status": "os", "release_url": None, "note": None}
            devices.setdefault(dev, []).append(entry)
            drift = extra.get("drift")
            if isinstance(drift, dict):
                status = str(drift.get("status") or "UNKNOWN").upper()
                reported_at = r.get("reported_at")
                if isinstance(reported_at, datetime):
                    report_time = reported_at if reported_at.tzinfo else reported_at.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - report_time > timedelta(hours=36):
                        status = "STALE"
                    reported_at = report_time.isoformat()
                if status not in drift_counts:
                    status = "UNKNOWN"
                drift_counts[status] += 1
                drift_devices.append({
                    "device": dev,
                    "status": status,
                    "checked_at": drift.get("checked_at"),
                    "last_reported_at": reported_at,
                    "reason": drift.get("reason"),
                    "checks": drift.get("checks") if isinstance(drift.get("checks"), list) else [],
                })
            continue
        if comp is None:
            entry = {"key": None, "name": r["image"], "current": r.get("tag"),
                     "latest": None, "status": "unregistered", "release_url": None,
                     "note": "未登记镜像"}
        else:
            up = upstream.get(comp["component_key"], {})
            latest = up.get("latest_version")
            current = device_extra.get(dev, {}).get(comp["component_key"], r.get("tag"))
            status = classify_component(family=comp["family"], upstream_source=comp["upstream_source"],
                                        current=current, latest=latest,
                                        version_pattern=comp.get("version_pattern"))
            if status == "own" and len(own_tags.get(comp["component_key"], set())) > 1:
                status = "own-mismatch"
            entry = {"key": comp["component_key"], "name": comp["display_name"],
                     "current": current, "latest": latest, "status": status,
                     "release_url": up.get("release_url"), "note": comp.get("pin_note")}
        summary[entry["status"]] = summary.get(entry["status"], 0) + 1
        devices.setdefault(dev, []).append(entry)

    overall = "ok"
    if summary["behind"] or summary["own-mismatch"] or drift_counts["FAIL"] or drift_counts["STALE"]:
        overall = "warning"
    summary["status"] = overall
    summary["drift"] = drift_counts
    return {"summary": summary,
            "devices": [{"device": d, "components": c} for d, c in sorted(devices.items())],
            "drift": sorted(drift_devices, key=lambda item: item["device"])}


@router.get("/v1/ops/versions")
def ops_versions(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT component_key, display_name, kind, match_images, devices, "
                        "upstream_source, upstream_ref, version_pattern, pin_note, family "
                        "FROM version_components WHERE active ORDER BY sort_order")
            comps = [dict(zip(
                ["component_key", "display_name", "kind", "match_images", "devices",
                 "upstream_source", "upstream_ref", "version_pattern", "pin_note", "family"], row))
                for row in cur.fetchall()]
            cur.execute("SELECT device, image, tag, digest, extra_json, reported_at FROM version_reports")
            reports = [{"device": r[0], "image": r[1], "tag": r[2], "digest": r[3],
                        "extra": r[4] or {}, "reported_at": r[5]} for r in cur.fetchall()]
            cur.execute("SELECT component_key, latest_version, release_url, checked_at, "
                        "check_status, check_error FROM version_upstream_state")
            upstream = {r[0]: {"latest_version": r[1], "release_url": r[2],
                               "checked_at": r[3].isoformat() if r[3] else None,
                               "check_status": r[4], "check_error": r[5]} for r in cur.fetchall()}
    return build_inventory(reports, comps, upstream)


def render_digest_text(inventory: dict, stale_devices: list[str]) -> str:
    behind, mismatch, unregistered = [], [], []
    for d in inventory.get("devices", []):
        for c in d["components"]:
            line = f"  · {d['device']}/{c['name']}：{c.get('current')} → {c.get('latest')}"
            if c["status"] == "behind":
                behind.append(line + (f"（{c['release_url']}）" if c.get("release_url") else ""))
            elif c["status"] == "own-mismatch":
                mismatch.append(f"  · {c['name']}：{d['device']} tag={c.get('current')}")
            elif c["status"] == "unregistered":
                unregistered.append(f"  · {d['device']}/{c['name']}={c.get('current')}")
    parts = ["📦 每周版本巡检"]
    if behind:
        parts.append("🔴 有新版本可用：\n" + "\n".join(behind))
    if mismatch:
        parts.append("🟠 自家镜像跨设备 tag 不一致：\n" + "\n".join(mismatch))
    if unregistered:
        parts.append("⚠️ 未登记镜像（新部署？请补进组件表）：\n" + "\n".join(unregistered))
    if stale_devices:
        parts.append("⛔ 采集未上报（管道可能故障）：" + "、".join(stale_devices))
    if len(parts) == 1:
        total = sum(len(d["components"]) for d in inventory.get("devices", []))
        parts.append(f"✅ 全部最新，{total} 个组件已核对。")
    return "\n\n".join(parts)


@router.post("/v1/internal/versions/weekly-digest")
def weekly_digest(_: None = Depends(_require_report_token)) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT component_key, display_name, kind, match_images, devices, "
                        "upstream_source, upstream_ref, version_pattern, pin_note, family "
                        "FROM version_components WHERE active ORDER BY sort_order")
            comps = [dict(zip(
                ["component_key", "display_name", "kind", "match_images", "devices",
                 "upstream_source", "upstream_ref", "version_pattern", "pin_note", "family"], row))
                for row in cur.fetchall()]
            cur.execute("SELECT device, image, tag, digest, extra_json, reported_at FROM version_reports")
            reports = [{"device": r[0], "image": r[1], "tag": r[2], "digest": r[3],
                        "extra": r[4] or {}, "reported_at": r[5]} for r in cur.fetchall()]
            cur.execute("SELECT component_key, latest_version, release_url FROM version_upstream_state")
            upstream = {r[0]: {"latest_version": r[1], "release_url": r[2]} for r in cur.fetchall()}
            cur.execute("SELECT device, MAX(reported_at) FROM version_reports GROUP BY device")
            seen = {r[0]: r[1] for r in cur.fetchall()}
    inv = build_inventory(reports, comps, upstream)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    expected = {"aliecs", "webdock1", "webdock2"}
    stale = sorted(d for d in expected if d not in seen or (seen[d] and seen[d] < cutoff))
    text = render_digest_text(inv, stale)
    # 收件人由 notify_routes 决定，不再读 VERSION_DIGEST_FEISHU_RECEIVE_ID。
    # 周报正文是排好版的文本（含 | 与对齐空格），必须按 preformatted 走，
    # 否则会被各家 markdown 重排。
    result = dispatch.deliver(
        Notification.model_validate(
            {
                "source": "aliecs-versions",
                "event": "weekly_digest",
                "level": "warn" if stale else "info",
                "title": "版本周报",
                "segments": [{"kind": "text", "text": text, "preformatted": True}],
                "dedup_key": f"versions:weekly:{datetime.now(timezone.utc).strftime('%G-W%V')}",
            }
        )
    )
    return {"ok": True, "sent": bool(result.get("sent")), "stale": stale}
