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

from . import config, git_ops
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


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "coding-executor", "version": "0.1.0"}


@app.get("/repos", dependencies=[Depends(require_token)])
def list_repos() -> dict:
    return {
        "repos": [{"name": r.name, "path": str(r.path)} for r in _REPOS.values()],
        "allowed_actions": list(git_ops.READ_ONLY_ACTIONS),
        "phase": "phase-2-readonly",
    }


@app.post("/tasks", dependencies=[Depends(require_token)])
def create_task(req: TaskRequest) -> dict:
    repo = _REPOS.get(req.repo)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"仓库不在白名单：{req.repo!r}")
    if req.action not in git_ops.READ_ONLY_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action 不允许（阶段二仅只读）：{req.action!r}",
        )
    job = _STORE.submit(repo.name, repo.path, req.action, req.params or {})
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
