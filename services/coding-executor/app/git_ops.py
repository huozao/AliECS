"""Read-only git operations for phase 2 (dry-run only).

Every action here is non-mutating: no checkout, no apply, no commit, no fetch.
Each action builds a fixed argv (never a shell string) and runs it inside the
repo with a timeout. User-supplied values are validated before they reach the
command line. Output is truncated so a huge diff cannot exhaust memory.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

MAX_OUTPUT_BYTES = 200_000
MAX_LOG_COUNT = 200
DEFAULT_LOG_COUNT = 30
GIT_TIMEOUT_SECONDS = 30

# A git ref / pathspec we are willing to pass through. Deliberately strict:
# no spaces, no shell metacharacters, no leading dash (option injection).
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9_./@^~-]{1,200}$")

READ_ONLY_ACTIONS = ("git_status", "git_log", "git_diff", "list_files", "read_file")


class ActionError(ValueError):
    """Raised for invalid action input; surfaced to the caller as a 4xx-style
    job failure rather than a crash."""


def _validate_ref(value: str, *, field: str) -> str:
    value = value.strip()
    if not SAFE_REF_RE.match(value) or value.startswith("-"):
        raise ActionError(f"{field} 含非法字符或以连字符开头：{value!r}")
    return value


def _truncate(raw: bytes) -> tuple[str, bool]:
    truncated = len(raw) > MAX_OUTPUT_BYTES
    body = raw[:MAX_OUTPUT_BYTES]
    return body.decode("utf-8", errors="replace"), truncated


def _run_git(repo_path: Path, args: list[str]) -> tuple[str, bool]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_path),
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ActionError(f"git {args[0]} 失败（exit {proc.returncode}）：{err[:500]}")
    return _truncate(proc.stdout)


def _resolve_within(repo_path: Path, rel: str) -> Path:
    """Resolve a user path and guarantee it stays inside the repo."""
    candidate = (repo_path / rel).resolve()
    if candidate != repo_path and repo_path not in candidate.parents:
        raise ActionError(f"路径越界，必须在仓库内：{rel!r}")
    return candidate


def run_action(repo_path: Path, action: str, params: dict) -> dict:
    params = params or {}

    if action == "git_status":
        out, truncated = _run_git(repo_path, ["status", "--porcelain=v1", "-b"])
        return {"action": action, "output": out, "truncated": truncated}

    if action == "git_log":
        count = params.get("count", DEFAULT_LOG_COUNT)
        if not isinstance(count, int) or not (1 <= count <= MAX_LOG_COUNT):
            raise ActionError(f"count 必须是 1..{MAX_LOG_COUNT} 的整数")
        out, truncated = _run_git(
            repo_path, ["log", f"-n{count}", "--oneline", "--no-color"]
        )
        return {"action": action, "output": out, "truncated": truncated}

    if action == "git_diff":
        args = ["diff", "--no-color"]
        ref = params.get("ref")
        if ref:
            args.append(_validate_ref(str(ref), field="ref"))
        path = params.get("path")
        if path:
            args += ["--", _validate_ref(str(path), field="path")]
        out, truncated = _run_git(repo_path, args)
        return {"action": action, "output": out, "truncated": truncated}

    if action == "list_files":
        args = ["ls-files"]
        subdir = params.get("path")
        if subdir:
            args.append(_validate_ref(str(subdir), field="path"))
        out, truncated = _run_git(repo_path, args)
        return {"action": action, "output": out, "truncated": truncated}

    if action == "read_file":
        rel = params.get("path")
        if not rel or not isinstance(rel, str):
            raise ActionError("read_file 需要 path 参数")
        target = _resolve_within(repo_path, rel)
        if not target.is_file():
            raise ActionError(f"文件不存在：{rel!r}")
        raw = target.read_bytes()
        body, truncated = _truncate(raw)
        return {"action": action, "output": body, "truncated": truncated}

    raise ActionError(f"未知或不允许的 action：{action!r}（阶段二仅只读）")
