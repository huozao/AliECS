"""In-memory async job store.

Tasks run on a small thread pool so the MCP tool call returns a job id quickly
and ChatGPT polls for the result. A future phase that adds long-running coding
agents will swap this for a durable store; for read-only dry-run it is enough.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import git_ops, worktree_ops

MAX_JOBS_RETAINED = 200


@dataclass
class Job:
    id: str
    repo: str
    action: str
    params: dict
    status: str = "pending"  # pending -> running -> done | error
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "repo": self.repo,
            "action": self.action,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class JobStore:
    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def submit(self, repo: str, repo_path: Path, action: str, params: dict) -> Job:
        job = Job(id=uuid.uuid4().hex, repo=repo, action=action, params=params or {})
        with self._lock:
            self._jobs[job.id] = job
            self._evict_locked()
        self._pool.submit(self._run, job, repo_path)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(self, job: Job, repo_path: Path) -> None:
        with self._lock:
            job.status = "running"
        try:
            if job.action in git_ops.READ_ONLY_ACTIONS:
                result = git_ops.run_action(repo_path, job.action, job.params)
            elif job.action in worktree_ops.WRITE_ACTIONS:
                result = worktree_ops.run_write_action(repo_path, job.action, job.params)
            else:
                raise git_ops.ActionError(f"未知 action：{job.action!r}")
            with self._lock:
                job.result = result
                job.status = "done"
        except Exception as exc:  # noqa: BLE001 - report any failure as job error
            with self._lock:
                job.error = str(exc)
                job.status = "error"
        finally:
            with self._lock:
                job.finished_at = time.time()

    def _evict_locked(self) -> None:
        if len(self._jobs) <= MAX_JOBS_RETAINED:
            return
        oldest = sorted(self._jobs.values(), key=lambda j: j.created_at)
        for job in oldest[: len(self._jobs) - MAX_JOBS_RETAINED]:
            self._jobs.pop(job.id, None)
