"""Worktree-isolated write primitives for phase 3a.

Every write happens inside a dedicated `git worktree` checked out on a
throwaway branch `codex-task-<task_id>`, never on the repo's main checkout.
Callers create a worktree, run one or more write actions inside it, inspect
the diff, and either keep it (for a human to merge manually later) or discard
it. Nothing here pushes, merges, or touches the original working tree.
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

from .git_ops import ActionError, GIT_TIMEOUT_SECONDS, MAX_OUTPUT_BYTES, _resolve_within, _run_git, _truncate

WRITE_ACTIONS = ("write_file", "apply_patch", "git_commit", "git_diff_worktree")

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

COMMIT_IDENTITY = ["-c", "user.email=codex@aliecs.local", "-c", "user.name=codex-bot"]

_REGISTRY: dict[tuple[str, str], Path] = {}
_LOCK = threading.Lock()


def _validate_task_id(task_id: str) -> str:
    task_id = task_id.strip()
    if not TASK_ID_RE.match(task_id):
        raise ActionError(f"task_id 含非法字符：{task_id!r}（仅允许字母数字、-、_，长度 1..64）")
    return task_id


def _branch_name(task_id: str) -> str:
    return f"codex-task-{task_id}"


def _worktree_root(repo_path: Path) -> Path:
    return repo_path.parent / f"{repo_path.name}-codex-worktrees"


def create_worktree(repo_name: str, repo_path: Path, task_id: str, base_ref: str = "HEAD") -> Path:
    task_id = _validate_task_id(task_id)
    key = (repo_name, task_id)

    with _LOCK:
        if key in _REGISTRY:
            raise ActionError(f"task_id 已存在 worktree：{task_id!r}")

    worktree_root = _worktree_root(repo_path)
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root / task_id
    if worktree_path.exists():
        raise ActionError(f"worktree 目录已存在：{worktree_path}")

    branch = _branch_name(task_id)
    proc = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), base_ref],
        cwd=str(repo_path),
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ActionError(f"git worktree add 失败（exit {proc.returncode}）：{err[:500]}")

    with _LOCK:
        _REGISTRY[key] = worktree_path
    return worktree_path


def get_worktree(repo_name: str, task_id: str) -> Path | None:
    task_id = _validate_task_id(task_id)
    with _LOCK:
        return _REGISTRY.get((repo_name, task_id))


def remove_worktree(repo_name: str, repo_path: Path, task_id: str) -> None:
    task_id = _validate_task_id(task_id)
    key = (repo_name, task_id)
    with _LOCK:
        worktree_path = _REGISTRY.pop(key, None)
    if worktree_path is None:
        raise ActionError(f"worktree 不存在：{task_id!r}")

    proc = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=str(repo_path),
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ActionError(f"git worktree remove 失败（exit {proc.returncode}）：{err[:500]}")

    branch = _branch_name(task_id)
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=str(repo_path),
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _validate_patch_paths(worktree_path: Path, patch_text: str) -> None:
    """Run `git apply --numstat` on the patch and ensure every touched path
    stays inside the worktree. Raises ActionError if anything would escape."""
    proc = subprocess.run(
        ["git", "apply", "--numstat", "-"],
        cwd=str(worktree_path),
        input=patch_text.encode("utf-8"),
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ActionError(f"patch 格式无效：{err[:500]}")

    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        rel_path = parts[2].strip()
        _resolve_within(worktree_path, rel_path)


def run_write_action(worktree_path: Path, action: str, params: dict) -> dict:
    params = params or {}

    if action == "write_file":
        rel = params.get("path")
        content = params.get("content")
        if not rel or not isinstance(rel, str):
            raise ActionError("write_file 需要 path 参数")
        if content is None or not isinstance(content, str):
            raise ActionError("write_file 需要 content（字符串）参数")
        target = _resolve_within(worktree_path, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"action": action, "path": rel, "bytes_written": len(content.encode("utf-8"))}

    if action == "apply_patch":
        patch_text = params.get("patch")
        if not patch_text or not isinstance(patch_text, str):
            raise ActionError("apply_patch 需要 patch（unified diff 字符串）参数")
        _validate_patch_paths(worktree_path, patch_text)
        proc = subprocess.run(
            ["git", "apply", "-"],
            cwd=str(worktree_path),
            input=patch_text.encode("utf-8"),
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise ActionError(f"git apply 失败（exit {proc.returncode}）：{err[:500]}")
        return {"action": action, "applied": True}

    if action == "git_commit":
        message = params.get("message")
        if not message or not isinstance(message, str):
            raise ActionError("git_commit 需要 message 参数")
        add_proc = subprocess.run(
            ["git", "add", "-A"],
            cwd=str(worktree_path),
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if add_proc.returncode != 0:
            err = add_proc.stderr.decode("utf-8", errors="replace").strip()
            raise ActionError(f"git add 失败（exit {add_proc.returncode}）：{err[:500]}")

        commit_proc = subprocess.run(
            ["git", *COMMIT_IDENTITY, "commit", "-q", "-m", message],
            cwd=str(worktree_path),
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if commit_proc.returncode != 0:
            err = commit_proc.stderr.decode("utf-8", errors="replace").strip()
            raise ActionError(f"git commit 失败（exit {commit_proc.returncode}）：{err[:500]}")
        return {"action": action, "committed": True, "message": message}

    if action == "git_diff_worktree":
        ref = params.get("ref") or "HEAD"
        out, truncated = _run_git(worktree_path, ["diff", "--no-color", ref])
        return {"action": action, "output": out, "truncated": truncated}

    raise ActionError(f"未知或不允许的写操作：{action!r}")
