"""Thin client to the 开发机 coding executor.

Uses stdlib urllib so the ECS container stays dependency-light. The executor is
reached through a reverse SSH tunnel (host.docker.internal:18091 by default).
All failures degrade to a structured "unavailable" result rather than raising,
so ChatGPT gets a useful message instead of an opaque tool error.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TIMEOUT = float(os.getenv("EXECUTOR_TIMEOUT_SECONDS", "20"))


class ExecutorUnavailable(Exception):
    pass


def _base_url() -> str:
    return os.getenv("EXECUTOR_BASE_URL", "").strip().rstrip("/")


def _token() -> str:
    token = os.getenv("EXECUTOR_TOKEN", "").strip()
    if not token:
        token_file = os.getenv("EXECUTOR_TOKEN_FILE", "").strip()
        if token_file and Path(token_file).is_file():
            token = Path(token_file).read_text(encoding="utf-8").strip()
    return token


def is_configured() -> bool:
    return bool(_base_url() and _token())


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    base = _base_url()
    if not base:
        raise ExecutorUnavailable("EXECUTOR_BASE_URL 未配置")
    token = _token()
    if not token:
        raise ExecutorUnavailable("EXECUTOR_TOKEN 未配置")

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ExecutorUnavailable(f"executor HTTP {exc.code}: {body[:300]}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ExecutorUnavailable(f"无法连接 executor（隧道是否在线？）：{exc}") from exc


def create_task(repo: str, action: str, params: dict | None) -> dict:
    return _request("POST", "/tasks", {"repo": repo, "action": action, "params": params or {}})


def get_task(job_id: str) -> dict:
    return _request("GET", f"/tasks/{job_id}")


def list_targets() -> dict:
    return _request("GET", "/repos")
