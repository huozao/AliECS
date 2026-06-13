# MCP Coding Route Phase 3a: Worktree-Isolated Writes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the ChatGPT MCP coding bridge (`mcp-coding-server`) ask the 开发机 `coding-executor` to make file edits and commits, but only inside a disposable `git worktree` on a dedicated branch — never on the user's currently checked-out branch, and never auto-pushed or auto-merged.

**Architecture:** `coding-executor` gains a new `worktree_ops` module: create a `git worktree` on branch `codex-task-<id>`, run a small set of write actions inside it (`write_file`, `apply_patch`, `git_commit`, `git_diff_worktree`), and let the operator inspect the diff or discard the worktree later. `mcp-coding-server` exposes this as new MCP tools and bumps its phase to `phase-3a-worktree-writes`. Phase 3b (a headless autonomous coding-agent loop that drives multi-step edits without a human reviewing each diff) is explicitly **out of scope** — see "Scoping note" below.

**Tech Stack:** FastAPI (`services/coding-executor`), `git worktree` / `git apply` / `git commit` via `subprocess` (no shell), FastMCP (`services/mcp-coding-server`), stdlib `urllib` executor client, `unittest` with the existing importlib-based package-loading pattern (avoids the `app` package name collision documented in `tests/test_coding_executor.py` and `tests/test_mcp_coding_server.py`).

---

## Scoping note (please confirm before/while executing)

The original "阶段三：真执行" idea covered two different things:

- **Phase 3a (this plan):** give the executor *write primitives* (write a file, apply a patch, commit) but only inside an isolated `git worktree` on a throwaway branch. A human (or a separate tool) still decides *what* to write — ChatGPT calls these primitives directly, one at a time, and a human reviews `get_coding_worktree_diff` before anything is merged.
- **Phase 3b (deferred, not in this plan):** a headless coding agent (e.g. Claude Code / Cline running unattended on 开发机) that takes a high-level task description and autonomously makes a whole series of edits/commits with no per-step review.

Phase 3b is a separate, larger design decision (agent loop, cost controls, how/when a human reviews the result, whether it can run while you're away) and is **not** included here. If you want it, it needs its own brainstorming session. This plan only adds the safe, inspectable primitives that such an agent (or a human via ChatGPT) could later be built on top of.

---

## Task 1: `worktree_ops.py` — create/remove worktrees and run write actions

**Files:**
- Create: `services/coding-executor/app/worktree_ops.py`

- [ ] **Step 1: Implement the module**

```python
# services/coding-executor/app/worktree_ops.py
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
```

Note: `git_ops._resolve_within`, `git_ops._run_git`, and `git_ops._truncate` are name-mangled with a leading underscore but are module-level functions, not class-private — importing them directly from `.git_ops` is consistent with how `jobs.py` already imports `git_ops` functions in this package. `MAX_OUTPUT_BYTES` is imported but unused directly (kept for symmetry with `git_ops`'s truncation contract); if `ruff`/flake8 complains about the unused import in Step 3's lint check, remove it from the `from .git_ops import ...` line.

- [ ] **Step 2: Verify the module imports cleanly**

Run:

```powershell
cd services/coding-executor
python -c "import sys; sys.path.insert(0,'.'); from app import worktree_ops; print(worktree_ops.WRITE_ACTIONS)"
```

Expected: `('write_file', 'apply_patch', 'git_commit', 'git_diff_worktree')`

- [ ] **Step 3: Commit**

```bash
git add services/coding-executor/app/worktree_ops.py
git commit -m "feat(coding-executor): add worktree-isolated write primitives"
```

## Task 2: Wire write actions into `jobs.py`

**Files:**
- Modify: `services/coding-executor/app/jobs.py:1-20` (imports)
- Modify: `services/coding-executor/app/jobs.py:66-77` (`JobStore._run`)

- [ ] **Step 1: Add the `worktree_ops` import**

In `services/coding-executor/app/jobs.py`, change line 18:

```python
from . import git_ops
```

to:

```python
from . import git_ops, worktree_ops
```

- [ ] **Step 2: Dispatch write actions in `_run`**

Replace the body of `_run` (lines 66-80):

```python
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
```

- [ ] **Step 3: Run the existing executor test suite to confirm no regression**

Run: `python -m unittest tests.test_coding_executor -v`
Expected: PASS (existing read-only tests still pass — `JobStore._run` behavior for `READ_ONLY_ACTIONS` is unchanged).

- [ ] **Step 4: Commit**

```bash
git add services/coding-executor/app/jobs.py
git commit -m "feat(coding-executor): dispatch worktree write actions from JobStore"
```

## Task 3: `coding-executor` HTTP API — worktree lifecycle + `/tasks` extension

**Files:**
- Modify: `services/coding-executor/app/main.py`

- [ ] **Step 1: Add imports and request models**

In `services/coding-executor/app/main.py`, change line 16:

```python
from . import config, git_ops
```

to:

```python
from . import config, git_ops, worktree_ops
```

After the `TaskRequest` class (lines 35-38), add:

```python
class WorktreeCreateRequest(BaseModel):
    repo: str
    task_id: str
    base_ref: str = "HEAD"
```

- [ ] **Step 2: Add worktree lifecycle endpoints**

After the `list_repos` endpoint (after line 52, before `create_task`), add:

```python
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
```

- [ ] **Step 3: Extend `/tasks` to accept write actions scoped to a worktree**

Replace `create_task` (lines 55-66):

```python
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
```

- [ ] **Step 4: Update `/repos` to report both action sets**

Replace `list_repos` (lines 46-52):

```python
@app.get("/repos", dependencies=[Depends(require_token)])
def list_repos() -> dict:
    return {
        "repos": [{"name": r.name, "path": str(r.path)} for r in _REPOS.values()],
        "read_only_actions": list(git_ops.READ_ONLY_ACTIONS),
        "write_actions": list(worktree_ops.WRITE_ACTIONS),
        "phase": "phase-3a-worktree-writes",
    }
```

- [ ] **Step 5: Run the existing executor test suite**

Run: `python -m unittest tests.test_coding_executor -v`
Expected: PASS — no test asserts on the exact `/repos` payload shape (verify with `grep -n "allowed_actions\|phase-2-readonly" tests/test_coding_executor.py`; if it does, update that assertion to match the new keys).

- [ ] **Step 6: Commit**

```bash
git add services/coding-executor/app/main.py
git commit -m "feat(coding-executor): add worktree lifecycle endpoints and write-task dispatch"
```

## Task 4: Tests for `worktree_ops` and the executor wiring

**Files:**
- Create: `tests/test_coding_executor_worktree.py`

- [ ] **Step 1: Write the test file**

```python
# tests/test_coding_executor_worktree.py
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVC = ROOT / "services" / "coding-executor" / "app"

_PKG = "coding_executor_worktree_pkg"


def _load_pkg() -> dict:
    spec = importlib.util.spec_from_file_location(
        _PKG, SVC / "__init__.py", submodule_search_locations=[str(SVC)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = pkg
    spec.loader.exec_module(pkg)
    mods = {}
    for name in ("config", "git_ops", "worktree_ops", "jobs"):
        sub = importlib.util.spec_from_file_location(f"{_PKG}.{name}", SVC / f"{name}.py")
        module = importlib.util.module_from_spec(sub)
        sys.modules[f"{_PKG}.{name}"] = module
        sub.loader.exec_module(module)
        mods[name] = module
    return mods


_MODS = _load_pkg()
git_ops = _MODS["git_ops"]
worktree_ops = _MODS["worktree_ops"]
jobs = _MODS["jobs"]


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", *env_args, "add", "."], cwd=repo, check=True)
    subprocess.run(["git", *env_args, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


class WorktreeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _make_repo(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_worktree_checks_out_dedicated_branch(self) -> None:
        path = worktree_ops.create_worktree("good", self.repo, "task1")
        self.assertTrue(path.is_dir())
        self.assertTrue((path / "hello.txt").is_file())

        branch, _truncated = git_ops._run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
        self.assertEqual(branch.strip(), "codex-task-task1")

        # original checkout untouched
        main_branch, _ = git_ops._run_git(self.repo, ["rev-parse", "--abbrev-ref", "HEAD"])
        self.assertEqual(main_branch.strip(), "main")

    def test_duplicate_task_id_rejected(self) -> None:
        worktree_ops.create_worktree("good", self.repo, "task1")
        with self.assertRaises(git_ops.ActionError):
            worktree_ops.create_worktree("good", self.repo, "task1")

    def test_invalid_task_id_rejected(self) -> None:
        with self.assertRaises(git_ops.ActionError):
            worktree_ops.create_worktree("good", self.repo, "../escape")

    def test_remove_worktree_cleans_up(self) -> None:
        path = worktree_ops.create_worktree("good", self.repo, "task1")
        worktree_ops.remove_worktree("good", self.repo, "task1")
        self.assertFalse(path.exists())
        self.assertIsNone(worktree_ops.get_worktree("good", "task1"))


class WriteActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _make_repo(Path(self._tmp.name))
        self.worktree = worktree_ops.create_worktree("good", self.repo, "task1")

    def tearDown(self) -> None:
        worktree_ops.remove_worktree("good", self.repo, "task1")
        self._tmp.cleanup()

    def test_write_file_then_commit_then_diff(self) -> None:
        worktree_ops.run_write_action(self.worktree, "write_file", {"path": "new.txt", "content": "hi\n"})
        self.assertEqual((self.worktree / "new.txt").read_text(encoding="utf-8"), "hi\n")

        commit_result = worktree_ops.run_write_action(self.worktree, "git_commit", {"message": "add new.txt"})
        self.assertTrue(commit_result["committed"])

        diff = worktree_ops.run_write_action(self.worktree, "git_diff_worktree", {"ref": "main"})
        self.assertIn("new.txt", diff["output"])

    def test_write_file_blocks_path_traversal(self) -> None:
        with self.assertRaises(git_ops.ActionError):
            worktree_ops.run_write_action(self.worktree, "write_file", {"path": "../escape.txt", "content": "x"})

    def test_apply_patch_blocks_paths_outside_worktree(self) -> None:
        patch = (
            "diff --git a/../escape.txt b/../escape.txt\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
            "--- /dev/null\n"
            "+++ b/../escape.txt\n"
            "@@ -0,0 +1 @@\n"
            "+evil\n"
        )
        with self.assertRaises(git_ops.ActionError):
            worktree_ops.run_write_action(self.worktree, "apply_patch", {"patch": patch})

    def test_unknown_write_action_rejected(self) -> None:
        with self.assertRaises(git_ops.ActionError):
            worktree_ops.run_write_action(self.worktree, "delete_repo", {})


class JobStoreWriteDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _make_repo(Path(self._tmp.name))
        self.worktree = worktree_ops.create_worktree("good", self.repo, "task1")

    def tearDown(self) -> None:
        worktree_ops.remove_worktree("good", self.repo, "task1")
        self._tmp.cleanup()

    def _wait(self, store, job_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = store.get(job_id)
            if job and job.status in ("done", "error"):
                return job
            time.sleep(0.05)
        self.fail("job did not finish in time")

    def test_write_file_job_runs_to_done(self) -> None:
        store = jobs.JobStore()
        job = store.submit("good", self.worktree, "write_file", {"path": "a.txt", "content": "hi\n"})
        finished = self._wait(store, job.id)
        self.assertEqual(finished.status, "done")
        self.assertEqual((self.worktree / "a.txt").read_text(encoding="utf-8"), "hi\n")

    def test_unknown_action_job_records_error(self) -> None:
        store = jobs.JobStore()
        job = store.submit("good", self.worktree, "rm_rf", {})
        finished = self._wait(store, job.id)
        self.assertEqual(finished.status, "error")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `python -m unittest tests.test_coding_executor_worktree -v`
Expected: PASS (9 tests). Note that `_run_git` is a module-level function in `git_ops`, accessible as `git_ops._run_git` from the test module just like `git_ops.ActionError` and `git_ops.READ_ONLY_ACTIONS` are accessed in the existing `tests/test_coding_executor.py`.

- [ ] **Step 3: Run the full repo test suite**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, no regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/test_coding_executor_worktree.py
git commit -m "test(coding-executor): cover worktree lifecycle and write actions"
```

## Task 5: `mcp-coding-server` executor client — worktree wrappers

**Files:**
- Modify: `services/mcp-coding-server/app/executor_client.py`

- [ ] **Step 1: Add the new client functions**

At the end of `services/mcp-coding-server/app/executor_client.py`, after `list_targets`, add:

```python
def create_worktree(repo: str, task_id: str, base_ref: str = "HEAD") -> dict:
    return _request("POST", "/worktrees", {"repo": repo, "task_id": task_id, "base_ref": base_ref})


def discard_worktree(repo: str, task_id: str) -> dict:
    return _request("DELETE", f"/worktrees/{repo}/{task_id}")


def get_worktree_diff(repo: str, task_id: str, ref: str = "HEAD") -> dict:
    return _request("POST", "/tasks", {"repo": repo, "action": "git_diff_worktree", "params": {"task_id": task_id, "ref": ref}})
```

`_request` already supports `DELETE` because it only special-cases the presence of a JSON `payload` (see `urllib.request.Request(..., data=data, method=method)`); a `DELETE` with no body works the same as the existing `GET` calls.

- [ ] **Step 2: Run the existing mcp-coding-server test suite**

Run: `python -m unittest tests.test_mcp_coding_server -v`
Expected: PASS (no behavior change yet — these are new unused functions until Task 6).

- [ ] **Step 3: Commit**

```bash
git add services/mcp-coding-server/app/executor_client.py
git commit -m "feat(mcp-coding-server): add executor client wrappers for worktree lifecycle"
```

## Task 6: `mcp-coding-server` — new MCP tools and phase bump

**Files:**
- Modify: `services/mcp-coding-server/app/main.py`

- [ ] **Step 1: Bump phase and tool list**

Change lines 30 and the `"tools"` list in `server_info_payload` (line 51-57):

```python
PHASE = "phase-3a-worktree-writes"
```

```python
        "tools": [
            "ping",
            "server_info",
            "list_coding_targets",
            "start_coding_task",
            "get_coding_task",
            "create_coding_worktree",
            "discard_coding_worktree",
            "get_coding_worktree_diff",
        ],
        "note": (
            "阶段三 a：只读 git 操作仍是 dry-run；写操作（write_file / apply_patch / "
            "git_commit）必须先用 create_coding_worktree 创建隔离 worktree，"
            "在该 worktree 分支上进行，绝不直接修改主工作区，也不会自动 push/merge。"
        ),
```

- [ ] **Step 2: Update the server instructions string**

Replace the `instructions=(...)` block passed to `FastMCP(...)` (lines 64-77):

```python
mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "AliECS 编程桥接服务，阶段三 a（worktree 隔离写入）。\n"
        "- ping / server_info：连通性与状态，只读。\n"
        "- list_coding_targets：列出可操作的仓库白名单、只读操作与写操作。\n"
        "- start_coding_task：发起只读 git 任务（git_status / git_log / git_diff / "
        "list_files / read_file），返回任务 id，用 get_coding_task 查询结果。\n"
        "- create_coding_worktree：为某仓库创建一个隔离的 git worktree（分支名 "
        "codex-task-<task_id>），写操作必须先调用本工具。\n"
        "- 写操作（write_file / apply_patch / git_commit）通过 start_coding_task 发起，"
        "params 必须包含上一步返回的 task_id，且只作用于该 worktree。\n"
        "- get_coding_worktree_diff：查看某 worktree 相对 base ref 的 diff，供人工审阅。\n"
        "- discard_coding_worktree：丢弃某 worktree 及其分支，不可恢复。\n"
        "本阶段绝不直接修改用户当前签出的分支，也绝不自动 push 或 merge；"
        "所有写入只发生在 codex-task-<task_id> 分支的独立 worktree 中。"
    ),
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8090")),
    stateless_http=True,
    json_response=True,
)
```

- [ ] **Step 3: Add the new tools**

After `get_coding_task` (after line 141), add:

```python
@mcp.tool(annotations=TASK_START)
def create_coding_worktree(repo: str, task_id: str, base_ref: str = "HEAD") -> str:
    """为指定仓库创建一个隔离的 git worktree，分支名为 codex-task-<task_id>。

    repo：list_coding_targets 返回的仓库名。
    task_id：自定义任务标识（仅允许字母数字、-、_，1..64 字符），同一仓库内必须唯一。
    base_ref：worktree 的起点引用，默认 HEAD。
    创建后，写操作（write_file / apply_patch / git_commit）通过 start_coding_task
    发起，params 必须带上这里的 task_id。本操作不影响用户当前签出的分支。
    """
    try:
        return json.dumps(executor_client.create_worktree(repo, task_id, base_ref), ensure_ascii=False)
    except executor_client.ExecutorUnavailable as exc:
        return _unavailable(str(exc))


@mcp.tool(annotations=READ_ONLY)
def get_coding_worktree_diff(repo: str, task_id: str, ref: str = "HEAD") -> str:
    """查看某个 worktree 相对 ref（默认 HEAD，即该 worktree 分支的起点）的 diff。

    用于在丢弃或合并前人工审阅 codex-task-<task_id> 分支上的改动。只读，无副作用。
    """
    try:
        return json.dumps(executor_client.get_worktree_diff(repo, task_id, ref), ensure_ascii=False)
    except executor_client.ExecutorUnavailable as exc:
        return _unavailable(str(exc))


@mcp.tool(annotations=TASK_START)
def discard_coding_worktree(repo: str, task_id: str) -> str:
    """丢弃某个 worktree 及其 codex-task-<task_id> 分支，不可恢复。

    在确认改动不需要保留，或已经通过其他方式（人工 cherry-pick 等）合并之后调用。
    """
    try:
        return json.dumps(executor_client.discard_worktree(repo, task_id), ensure_ascii=False)
    except executor_client.ExecutorUnavailable as exc:
        return _unavailable(str(exc))
```

- [ ] **Step 4: Update `start_coding_task`'s docstring to mention write actions**

Replace the docstring of `start_coding_task` (lines 120-126):

```python
def start_coding_task(repo: str, action: str, params: dict | None = None) -> str:
    """在开发机对指定仓库发起一个任务，返回任务 id 供后续轮询。

    repo：list_coding_targets 返回的仓库名。
    action：
      - 只读：git_status / git_log / git_diff / list_files / read_file。
      - 写入（必须先用 create_coding_worktree 创建 worktree）：write_file /
        apply_patch / git_commit / git_diff_worktree。写操作的 params 必须包含
        create_coding_worktree 返回的 task_id，且只作用于该 worktree 分支。
    params：例如 {"count": 20}、{"ref": "HEAD~1"}、{"path": "README.md"}，
      或写操作的 {"task_id": "...", "path": "...", "content": "..."}。
    只读操作不修改任何文件；写操作只修改 codex-task-<task_id> 分支的隔离 worktree，
    绝不修改用户当前签出的分支，也不会自动 push 或 merge。
    """
```

- [ ] **Step 5: Run the mcp-coding-server test suite**

Run: `python -m unittest tests.test_mcp_coding_server -v`
Expected: PASS — if `test_server_info_payload_shape` asserts an exact tool count or `PHASE` string, it will need updating (see Task 7).

- [ ] **Step 6: Commit**

```bash
git add services/mcp-coding-server/app/main.py
git commit -m "feat(mcp-coding-server): add worktree create/diff/discard tools, bump phase to 3a"
```

## Task 7: Update `mcp-coding-server` tests for the new phase/tools

**Files:**
- Modify: `tests/test_mcp_coding_server.py`

- [ ] **Step 1: Inspect the current assertions**

Run:

```powershell
Select-String -Path tests/test_mcp_coding_server.py -Pattern "PHASE|tools|phase-2"
```

- [ ] **Step 2: Update `test_server_info_payload_shape`**

If the test asserts `self.assertIn("start_coding_task", payload["tools"])` only, add three more assertions right after it (matching the existing test's indentation):

```python
        self.assertIn("create_coding_worktree", payload["tools"])
        self.assertIn("get_coding_worktree_diff", payload["tools"])
        self.assertIn("discard_coding_worktree", payload["tools"])
```

If the test instead asserts `payload["phase"] == "phase-2-dryrun"` as a literal string anywhere, update that literal to `"phase-3a-worktree-writes"` (it should already use `mcp_main.PHASE` per the existing `test_server_info_payload_shape` shown in this plan's research — in that case no change is needed, since `PHASE` was updated in Task 6 Step 1).

- [ ] **Step 3: Add a test for the new tool wrappers using a fake executor client**

Add this test class at the end of `tests/test_mcp_coding_server.py`, before the `if __name__ == "__main__":` block:

```python
class WorktreeToolTests(unittest.TestCase):
    def test_create_get_discard_worktree_round_trip(self) -> None:
        calls = []

        def fake_create_worktree(repo, task_id, base_ref="HEAD"):
            calls.append(("create", repo, task_id, base_ref))
            return {"repo": repo, "task_id": task_id, "branch": f"codex-task-{task_id}", "path": "/tmp/x"}

        def fake_get_worktree_diff(repo, task_id, ref="HEAD"):
            calls.append(("diff", repo, task_id, ref))
            return {"action": "git_diff_worktree", "output": "diff --git a/x b/x", "truncated": False}

        def fake_discard_worktree(repo, task_id):
            calls.append(("discard", repo, task_id))
            return {"repo": repo, "task_id": task_id, "removed": True}

        with unittest.mock.patch.object(mcp_main.executor_client, "create_worktree", fake_create_worktree), \
             unittest.mock.patch.object(mcp_main.executor_client, "get_worktree_diff", fake_get_worktree_diff), \
             unittest.mock.patch.object(mcp_main.executor_client, "discard_worktree", fake_discard_worktree):
            created = json.loads(mcp_main.create_coding_worktree.fn("aliecs", "task1"))
            diff = json.loads(mcp_main.get_coding_worktree_diff.fn("aliecs", "task1"))
            discarded = json.loads(mcp_main.discard_coding_worktree.fn("aliecs", "task1"))

        self.assertEqual(created["task_id"], "task1")
        self.assertIn("diff --git", diff["output"])
        self.assertTrue(discarded["removed"])
        self.assertEqual(calls, [
            ("create", "aliecs", "task1", "HEAD"),
            ("diff", "aliecs", "task1", "HEAD"),
            ("discard", "aliecs", "task1"),
        ])

    def test_create_worktree_unavailable_degrades_gracefully(self) -> None:
        def fake_create_worktree(repo, task_id, base_ref="HEAD"):
            raise mcp_main.executor_client.ExecutorUnavailable("no tunnel")

        with unittest.mock.patch.object(mcp_main.executor_client, "create_worktree", fake_create_worktree):
            result = json.loads(mcp_main.create_coding_worktree.fn("aliecs", "task1"))

        self.assertEqual(result["executor"], "unavailable")
```

This needs `import json` and `import unittest.mock` at the top of the file if not already present — check with:

```powershell
Select-String -Path tests/test_mcp_coding_server.py -Pattern "^import json|^import unittest.mock"
```

and add whichever is missing.

`mcp.tool`-decorated functions are `FunctionTool` objects in FastMCP; calling the underlying Python function directly via `.fn(...)` (as used here) matches how FastMCP itself invokes tools and avoids needing an MCP client/transport in the test. If `.fn` is not the correct attribute on this FastMCP version, run:

```powershell
python -c "from mcp.server.fastmcp import FastMCP; import inspect; print([a for a in dir(FastMCP) if 'tool' in a.lower()])"
```

and inspect `services/mcp-coding-server/app/main.py`'s existing tests for how `ping`/`server_info` (plain functions, not decorated at call time in tests) are invoked — `ping_payload`/`server_info_payload` are called directly because they're the undecorated helper functions. For the new tools there is no separate undecorated helper, so `.fn` (or the equivalent unwrap attribute found via the `dir()` check above) is required.

- [ ] **Step 4: Run the test suite**

Run: `python -m unittest tests.test_mcp_coding_server -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_coding_server.py
git commit -m "test(mcp-coding-server): cover worktree create/diff/discard tools"
```

## Task 8: Documentation — phase 3a safety model

**Files:**
- Modify: `services/coding-executor/README.md`
- Modify: `services/mcp-coding-server/app/main.py` (already updated via Task 6 — this task is docs-only for the README)

- [ ] **Step 1: Update `services/coding-executor/README.md`**

Replace the title line and "它不是什么" section (lines 1-9):

```markdown
# coding-executor（开发机本地服务，阶段三 a：worktree 隔离写入）

ChatGPT 连接器 → ECS `mcp-coding-server` → **反向 SSH 隧道** → 本服务（开发机）。

本服务对白名单仓库支持两类操作：

- **只读**（直接在仓库工作区执行）：`git_status` / `git_log` / `git_diff` /
  `list_files` / `read_file`。
- **写入**（仅在隔离 worktree 中执行，分支名 `codex-task-<task_id>`）：
  `write_file` / `apply_patch` / `git_commit` / `git_diff_worktree`。

写操作绝不直接修改用户当前签出的分支，也绝不自动 `push` 或 `merge`。调用顺序：

1. `POST /worktrees {"repo", "task_id", "base_ref"}` 创建隔离 worktree。
2. `POST /tasks {"repo", "action": "write_file"|..., "params": {"task_id": ..., ...}}`
   在该 worktree 中执行写操作。
3. `POST /tasks {"repo", "action": "git_diff_worktree", "params": {"task_id": ...}}`
   或 `GET /tasks/{id}` 查看结果，供人工审阅。
4. `DELETE /worktrees/{repo}/{task_id}` 丢弃 worktree 和分支（不可恢复），或保留
   分支供人工 `git fetch`/`cherry-pick`。

## 它不是什么

- 不进 ECS、不进 `compose.prod.yml`、不进 release 构建矩阵。它只跑在开发机。
- 不是自动编程 agent：每次写操作都是 ChatGPT 显式发起的单步操作，由人工通过
  `git_diff_worktree` 审阅；本阶段不包含无人值守的多步编辑循环（见项目计划中的
  "Phase 3b" 备注）。
- 不会修改用户当前签出的分支，不会 `push`，不会 `merge`。
```

- [ ] **Step 2: Add the new endpoints to the API table**

Replace the API table (the last section, starting at the `| 方法 | 路径 | 说明 |` line):

```markdown
## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查（免鉴权） |
| GET | `/repos` | 仓库白名单 + 只读/写操作列表 |
| POST | `/worktrees` | 创建隔离 worktree `{repo, task_id, base_ref}` → `{repo, task_id, branch, path}` |
| DELETE | `/worktrees/{repo}/{task_id}` | 丢弃 worktree 及分支（不可恢复） |
| POST | `/tasks` | 发起任务 `{repo, action, params}` → `{id, status}`；写操作的 `params` 必须含 `task_id` |
| GET | `/tasks/{id}` | 查询任务状态与结果 |
```

- [ ] **Step 3: Verify Markdown renders sensibly**

Run:

```powershell
Get-Content services/coding-executor/README.md | Select-String "^#"
```

Expected: a single `#` title line, two `##` sections (`它不是什么`, `本地运行`/etc — confirm the existing sections after line 9 are still intact and not accidentally duplicated by the edit).

- [ ] **Step 4: Commit**

```bash
git add services/coding-executor/README.md
git commit -m "docs(coding-executor): document phase 3a worktree write workflow"
```

---

## Self-Review

**Spec coverage:**

- Worktree create/remove + write actions (`write_file`, `apply_patch`, `git_commit`, `git_diff_worktree`) → Task 1.
- `JobStore` dispatch for write actions → Task 2.
- `coding-executor` HTTP surface (`/worktrees`, extended `/tasks`, `/repos`) → Task 3.
- Tests for worktree lifecycle, write actions, path-traversal/patch-escape guards, job dispatch → Task 4.
- `mcp-coding-server` executor client wrappers → Task 5.
- `mcp-coding-server` new tools + phase bump + instructions → Task 6.
- `mcp-coding-server` test updates → Task 7.
- Documentation of the phase 3a safety model → Task 8.
- Phase 3b scoping/deferral surfaced explicitly → "Scoping note" section above.

**Placeholder scan:** no TODO/TBD; every step has complete code, or an exact command plus what to look for (Task 7 Step 2/3 include explicit fallback instructions because the exact current test assertions and FastMCP tool-unwrap attribute weren't directly observed — but the fallback is a concrete `dir()` inspection command, not a vague "figure it out").

**Type/signature consistency:**

- `worktree_ops.create_worktree(repo_name, repo_path, task_id, base_ref="HEAD") -> Path` (Task 1) matches its call sites in `main.py` (Task 3) and the test (Task 4).
- `worktree_ops.get_worktree(repo_name, task_id) -> Path | None` and `remove_worktree(repo_name, repo_path, task_id) -> None` (Task 1) match Task 3's `/worktrees` endpoints.
- `worktree_ops.run_write_action(worktree_path, action, params) -> dict` (Task 1) matches `jobs.py`'s dispatch (Task 2) and the test (Task 4).
- `executor_client.create_worktree/get_worktree_diff/discard_worktree` (Task 5) match the MCP tool implementations (Task 6) and the test fakes (Task 7).
- `WRITE_ACTIONS = ("write_file", "apply_patch", "git_commit", "git_diff_worktree")` is defined once in `worktree_ops.py` and referenced (not redefined) everywhere else.

---

## Operator Prompt Template (Codex, unattended)

```text
You are executing an approved implementation plan end-to-end without stopping
for confirmation, except at the Hard Stop Conditions below.

Plan file: docs/superpowers/plans/2026-06-12-mcp-executor-worktree-writes.md
Repo root: this checkout of AliECS

IMPORTANT CONTEXT: This plan adds *write* capabilities to a service that is
reachable from ChatGPT over the internet (via a reverse SSH tunnel from
services/coding-executor on 开发机). Every write must stay confined to a
`git worktree` on a `codex-task-<id>` branch, as the plan specifies. Do not
relax any of the path-traversal or patch-path validation in worktree_ops.py
"to make a test pass" -- if a test fails because of validation, fix the test
input, not the guard.

Rules:
1. Work through Task 1 -> Task 8 in order; Tasks 1-4 are coding-executor side,
   Tasks 5-7 are mcp-coding-server side, Task 8 is docs. Task 2 depends on
   Task 1; Task 3 depends on Tasks 1-2; Task 4 depends on Task 3; Task 6
   depends on Task 5; Task 7 depends on Task 6.
2. After each task's final commit step, run:
     python -m unittest discover -s tests -v
   If it fails, fix the regression before moving to the next task.
3. Make one git commit per task step that says "Commit" -- do not batch
   multiple tasks into one commit.
4. Do not touch files outside this plan's "Files" lists.
5. Do not add new runtime dependencies.
6. If Task 7 Step 3's `.fn` attribute guess is wrong for the installed
   FastMCP version, use the `dir()` inspection command in that step to find
   the correct attribute, then proceed -- this is expected investigation,
   not a Hard Stop.

Hard Stop Conditions (stop and report instead of proceeding):
- Any step's "Run test to verify it fails/passes" produces an error message
  that doesn't match what the plan describes, AND you cannot resolve it by
  re-reading the referenced source file.
- `git push` is requested anywhere -- this plan only requires local commits.
  Do not push.
- You find yourself wanting to weaken `_resolve_within`, `_validate_patch_paths`,
  `TASK_ID_RE`, or any other safety guard in worktree_ops.py to make progress.
  Stop and report instead -- this is the one thing in this plan that must not
  be "fixed" by loosening.
- Any step would require a secret, credential, or production hostname.

When all 8 tasks are committed and the full suite passes, report:
- Which tasks completed.
- Final `git log --oneline -n 12`.
- Output of `python -m unittest discover -s tests -v` (last 20 lines).
- A reminder to the user that Phase 3b (headless autonomous agent loop) is
  still undesigned and out of scope.
```
