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
        self.repo_name = self.id().rsplit(".", 1)[-1]

    def tearDown(self) -> None:
        try:
            if worktree_ops.get_worktree(self.repo_name, "task1") is not None:
                worktree_ops.remove_worktree(self.repo_name, self.repo, "task1")
        except git_ops.ActionError:
            pass
        self._tmp.cleanup()

    def test_create_worktree_checks_out_dedicated_branch(self) -> None:
        path = worktree_ops.create_worktree(self.repo_name, self.repo, "task1")
        self.assertTrue(path.is_dir())
        self.assertTrue((path / "hello.txt").is_file())

        branch, _truncated = git_ops._run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
        self.assertEqual(branch.strip(), "codex-task-task1")

        # original checkout untouched
        main_branch, _ = git_ops._run_git(self.repo, ["rev-parse", "--abbrev-ref", "HEAD"])
        self.assertEqual(main_branch.strip(), "main")

    def test_duplicate_task_id_rejected(self) -> None:
        worktree_ops.create_worktree(self.repo_name, self.repo, "task1")
        with self.assertRaises(git_ops.ActionError):
            worktree_ops.create_worktree(self.repo_name, self.repo, "task1")

    def test_invalid_task_id_rejected(self) -> None:
        with self.assertRaises(git_ops.ActionError):
            worktree_ops.create_worktree(self.repo_name, self.repo, "../escape")

    def test_remove_worktree_cleans_up(self) -> None:
        path = worktree_ops.create_worktree(self.repo_name, self.repo, "task1")
        worktree_ops.remove_worktree(self.repo_name, self.repo, "task1")
        self.assertFalse(path.exists())
        self.assertIsNone(worktree_ops.get_worktree(self.repo_name, "task1"))


class WriteActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _make_repo(Path(self._tmp.name))
        self.repo_name = self.id().rsplit(".", 1)[-1]
        self.worktree = worktree_ops.create_worktree(self.repo_name, self.repo, "task1")

    def tearDown(self) -> None:
        try:
            if worktree_ops.get_worktree(self.repo_name, "task1") is not None:
                worktree_ops.remove_worktree(self.repo_name, self.repo, "task1")
        except git_ops.ActionError:
            pass
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
        self.repo_name = self.id().rsplit(".", 1)[-1]
        self.worktree = worktree_ops.create_worktree(self.repo_name, self.repo, "task1")

    def tearDown(self) -> None:
        try:
            if worktree_ops.get_worktree(self.repo_name, "task1") is not None:
                worktree_ops.remove_worktree(self.repo_name, self.repo, "task1")
        except git_ops.ActionError:
            pass
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
