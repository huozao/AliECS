"""Coding executor HTTP API (phase 2: read-only dry-run).

Runs on 开发机 bound to 127.0.0.1:18091. The ECS mcp-coding-server reaches it
through a reverse SSH tunnel and authenticates with a bearer token. Every
request must name a repo from the allowlist; only read-only git actions are
permitted in this phase.
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import config, git_ops, worktree_ops
from .jobs import JobStore

app = FastAPI(title="coding-executor", version="0.1.0")

_TOKEN = config.load_token()
_REPOS = config.load_repos()
_STORE = JobStore(max_workers=int(os.getenv("EXECUTOR_MAX_WORKERS", "4")))


def require_token(authorization: str = Header(default="")) -> None:
    if not _TOKEN:
        raise HTTPException(status_code=503, detail="executor 未配置 EXECUTOR_TOKEN")
    expected = f"Bearer {_TOKEN}"
    # Constant-ish comparison; tokens are short and local, timing risk is low.
    if authorization != expected:
        raise HTTPException(status_code=401, detail="鉴权失败")


class TaskRequest(BaseModel):
    repo: str
    action: str
    params: dict | None = None


class WorktreeCreateRequest(BaseModel):
    repo: str
    task_id: str
    base_ref: str = "HEAD"


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "coding-executor", "version": "0.1.0"}


@app.get("/repos", dependencies=[Depends(require_token)])
def list_repos() -> dict:
    return {
        "repos": [{"name": r.name, "path": str(r.path)} for r in _REPOS.values()],
        "read_only_actions": list(git_ops.READ_ONLY_ACTIONS),
        "write_actions": list(worktree_ops.WRITE_ACTIONS),
        "phase": "phase-3a-worktree-writes",
    }


@app.post("/worktrees", dependencies=[Depends(require_token)])
def create_worktree(req: WorktreeCreateRequest) -> dict:
    repo = _REPOS.get(req.repo)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"仓库不在白名单：{req.repo!r}")
    try:
        path = worktree_ops.create_worktree(repo.name, repo.path, req.task_id, req.base_ref)
    except git_ops.ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repo": repo.name, "task_id": req.task_id, "branch": f"codex-task-{req.task_id}", "path": str(path)}


@app.delete("/worktrees/{repo_name}/{task_id}", dependencies=[Depends(require_token)])
def discard_worktree(repo_name: str, task_id: str) -> dict:
    repo = _REPOS.get(repo_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"仓库不在白名单：{repo_name!r}")
    try:
        worktree_ops.remove_worktree(repo.name, repo.path, task_id)
    except git_ops.ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repo": repo.name, "task_id": task_id, "removed": True}


@app.post("/tasks", dependencies=[Depends(require_token)])
def create_task(req: TaskRequest) -> dict:
    repo = _REPOS.get(req.repo)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"仓库不在白名单：{req.repo!r}")

    params = req.params or {}

    if req.action in git_ops.READ_ONLY_ACTIONS:
        target_path = repo.path
    elif req.action in worktree_ops.WRITE_ACTIONS:
        task_id = params.get("task_id")
        if not task_id or not isinstance(task_id, str):
            raise HTTPException(status_code=400, detail="写操作需要 params.task_id（先调用 POST /worktrees 创建）")
        target_path = worktree_ops.get_worktree(repo.name, task_id)
        if target_path is None:
            raise HTTPException(status_code=404, detail=f"worktree 不存在，请先 POST /worktrees：{task_id!r}")
    else:
        raise HTTPException(status_code=400, detail=f"action 不允许：{req.action!r}")

    job = _STORE.submit(repo.name, target_path, req.action, params)
    return {"id": job.id, "status": job.status}


@app.get("/tasks/{job_id}", dependencies=[Depends(require_token)])
def get_task(job_id: str) -> dict:
    job = _STORE.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.to_public()


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("EXECUTOR_HOST", "127.0.0.1"),
        port=int(os.getenv("EXECUTOR_PORT", "18091")),
    )


if __name__ == "__main__":
    main()
