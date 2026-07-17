"""全设备版本看板：采集上报、上游对比、看板查询、周报推送。"""

from __future__ import annotations

import re

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
