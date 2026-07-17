"""全设备版本看板：采集上报、上游对比、看板查询、周报推送。"""

from __future__ import annotations

import hmac
import json
import os
import re
import urllib.request
from contextlib import closing
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.core import _conn, require_admin

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
                     Jsonb({"apt": body.apt, **body.extra})),
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
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO version_upstream_state(component_key, latest_version, release_url, "
                    "checked_at, check_status, check_error) VALUES (%s, %s, %s, NOW(), %s, %s) "
                    "ON CONFLICT (component_key) DO UPDATE SET latest_version=EXCLUDED.latest_version, "
                    "release_url=EXCLUDED.release_url, checked_at=NOW(), "
                    "check_status=EXCLUDED.check_status, check_error=EXCLUDED.check_error",
                    (key, normalize_version(latest) if latest else None, url, status, err),
                )
            checked += 1
        conn.commit()
    return {"ok": True, "checked": checked}
