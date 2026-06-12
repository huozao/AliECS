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

# Load the executor package under a synthetic name. `app` is already bound to
# backend-api in the shared unittest discovery, so importing by package name
# would grab the wrong module; registering submodules under a unique prefix
# also lets the package's relative imports (`from . import git_ops`) resolve.
_PKG = "coding_executor_pkg"


def _load_pkg() -> dict:
    spec = importlib.util.spec_from_file_location(
        _PKG, SVC / "__init__.py", submodule_search_locations=[str(SVC)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = pkg
    spec.loader.exec_module(pkg)
    mods = {}
    for name in ("config", "git_ops", "jobs"):
        sub = importlib.util.spec_from_file_location(f"{_PKG}.{name}", SVC / f"{name}.py")
        module = importlib.util.module_from_spec(sub)
        sys.modules[f"{_PKG}.{name}"] = module
        sub.loader.exec_module(module)
        mods[name] = module
    return mods


_MODS = _load_pkg()
config = _MODS["config"]
git_ops = _MODS["git_ops"]
jobs = _MODS["jobs"]


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", *env_args, "add", "."], cwd=repo, check=True)
    subprocess.run(["git", *env_args, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


class ConfigTests(unittest.TestCase):
    def test_parse_repos_accepts_valid_and_rejects_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            plain = root / "plain"
            plain.mkdir()
            spec = f"good={repo};bad={plain};missing={root/'nope'};malformed"
            repos = config.parse_repos(spec)
            self.assertEqual(set(repos), {"good"})
            self.assertEqual(repos["good"].path, repo.resolve())

    def test_load_token_prefers_env_then_file(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "tok"
            tf.write_text("filetok\n", encoding="utf-8")
            os.environ.pop("EXECUTOR_TOKEN", None)
            os.environ["EXECUTOR_TOKEN_FILE"] = str(tf)
            try:
                self.assertEqual(config.load_token(), "filetok")
                os.environ["EXECUTOR_TOKEN"] = "envtok"
                self.assertEqual(config.load_token(), "envtok")
            finally:
                os.environ.pop("EXECUTOR_TOKEN", None)
                os.environ.pop("EXECUTOR_TOKEN_FILE", None)


class GitOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _make_repo(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_git_status_and_log_and_listfiles(self) -> None:
        status = git_ops.run_action(self.repo, "git_status", {})
        self.assertIn("##", status["output"])
        log = git_ops.run_action(self.repo, "git_log", {"count": 5})
        self.assertIn("init", log["output"])
        files = git_ops.run_action(self.repo, "list_files", {})
        self.assertIn("hello.txt", files["output"])

    def test_read_file_within_repo(self) -> None:
        out = git_ops.run_action(self.repo, "read_file", {"path": "hello.txt"})
        self.assertEqual(out["output"].replace("\r\n", "\n"), "hello\n")

    def test_read_file_blocks_path_traversal(self) -> None:
        with self.assertRaises(git_ops.ActionError):
            git_ops.run_action(self.repo, "read_file", {"path": "../../etc/passwd"})

    def test_ref_validation_rejects_option_injection(self) -> None:
        with self.assertRaises(git_ops.ActionError):
            git_ops.run_action(self.repo, "git_diff", {"ref": "--output=/tmp/x"})
        with self.assertRaises(git_ops.ActionError):
            git_ops.run_action(self.repo, "git_diff", {"ref": "a; rm -rf b"})

    def test_log_count_bounds(self) -> None:
        with self.assertRaises(git_ops.ActionError):
            git_ops.run_action(self.repo, "git_log", {"count": 9999})

    def test_unknown_action_rejected(self) -> None:
        with self.assertRaises(git_ops.ActionError):
            git_ops.run_action(self.repo, "git_push", {})


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _make_repo(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _wait(self, store, job_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = store.get(job_id)
            if job and job.status in ("done", "error"):
                return job
            time.sleep(0.05)
        self.fail("job did not finish in time")

    def test_job_runs_to_done(self) -> None:
        store = jobs.JobStore()
        job = store.submit("good", self.repo, "git_status", {})
        finished = self._wait(store, job.id)
        self.assertEqual(finished.status, "done")
        self.assertIn("##", finished.result["output"])

    def test_job_records_error(self) -> None:
        store = jobs.JobStore()
        job = store.submit("good", self.repo, "read_file", {"path": "nope.txt"})
        finished = self._wait(store, job.id)
        self.assertEqual(finished.status, "error")
        self.assertIsNotNone(finished.error)


if __name__ == "__main__":
    unittest.main()
