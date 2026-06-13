from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVC = ROOT / "services" / "mcp-coding-server" / "app"

# Load as a synthetic package so the module's relative import
# (`from . import executor_client`) resolves, while avoiding the `app` package
# name already bound to backend-api in the shared unittest discovery.
_PKG = "mcp_coding_server_pkg"


def _load_main():
    spec = importlib.util.spec_from_file_location(
        _PKG, SVC / "__init__.py", submodule_search_locations=[str(SVC)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = pkg
    spec.loader.exec_module(pkg)
    for name in ("executor_client", "main"):
        sub = importlib.util.spec_from_file_location(f"{_PKG}.{name}", SVC / f"{name}.py")
        module = importlib.util.module_from_spec(sub)
        sys.modules[f"{_PKG}.{name}"] = module
        sub.loader.exec_module(module)
    return sys.modules[f"{_PKG}.main"]


mcp_main = _load_main()


class McpCodingServerTests(unittest.TestCase):
    def test_ping_payload_echoes_message(self) -> None:
        payload = mcp_main.ping_payload("你好，链路测试")
        self.assertEqual(payload["echo"], "你好，链路测试")
        self.assertEqual(payload["service"], mcp_main.SERVER_NAME)
        self.assertEqual(payload["version"], mcp_main.SERVER_VERSION)
        self.assertIn("server_time_utc", payload)

    def test_server_info_payload_shape(self) -> None:
        payload = mcp_main.server_info_payload()
        self.assertEqual(payload["phase"], mcp_main.PHASE)
        self.assertGreaterEqual(payload["uptime_seconds"], 0)
        self.assertIn("start_coding_task", payload["tools"])
        self.assertIn("create_coding_worktree", payload["tools"])
        self.assertIn("get_coding_worktree_diff", payload["tools"])
        self.assertIn("discard_coding_worktree", payload["tools"])
        self.assertIn("executor_configured", payload)

    def test_expected_tools_registered(self) -> None:
        tools = asyncio.run(mcp_main.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(
            names,
            {
                "ping",
                "server_info",
                "list_coding_targets",
                "start_coding_task",
                "get_coding_task",
                "create_coding_worktree",
                "get_coding_worktree_diff",
                "discard_coding_worktree",
            },
        )

    def test_readonly_annotations_are_correct(self) -> None:
        tools = {t.name: t for t in asyncio.run(mcp_main.mcp.list_tools())}
        for name in (
            "ping",
            "server_info",
            "list_coding_targets",
            "get_coding_task",
            "get_coding_worktree_diff",
        ):
            self.assertTrue(tools[name].annotations.readOnlyHint, f"{name} 应为只读")
        # Task-start tools reach the dev machine or mutate an isolated worktree,
        # so ChatGPT surfaces a confirmation modal.
        for name in ("start_coding_task", "create_coding_worktree", "discard_coding_worktree"):
            self.assertFalse(tools[name].annotations.readOnlyHint)

    def test_tools_degrade_gracefully_without_executor(self) -> None:
        import os

        os.environ.pop("EXECUTOR_BASE_URL", None)
        os.environ.pop("EXECUTOR_TOKEN", None)
        self.assertFalse(mcp_main.executor_client.is_configured())


class WorktreeToolTests(unittest.TestCase):
    def test_create_get_discard_worktree_round_trip(self) -> None:
        calls = []

        def fake_create_worktree(repo, task_id, base_ref="HEAD"):
            calls.append(("create", repo, task_id, base_ref))
            return {
                "repo": repo,
                "task_id": task_id,
                "branch": f"codex-task-{task_id}",
                "path": "/tmp/x",
            }

        def fake_get_worktree_diff(repo, task_id, ref="HEAD"):
            calls.append(("diff", repo, task_id, ref))
            return {
                "action": "git_diff_worktree",
                "output": "diff --git a/x b/x",
                "truncated": False,
            }

        def fake_discard_worktree(repo, task_id):
            calls.append(("discard", repo, task_id))
            return {"repo": repo, "task_id": task_id, "removed": True}

        with unittest.mock.patch.object(
            mcp_main.executor_client, "create_worktree", fake_create_worktree
        ), unittest.mock.patch.object(
            mcp_main.executor_client, "get_worktree_diff", fake_get_worktree_diff
        ), unittest.mock.patch.object(
            mcp_main.executor_client, "discard_worktree", fake_discard_worktree
        ):
            created = json.loads(mcp_main.create_coding_worktree("aliecs", "task1"))
            diff = json.loads(mcp_main.get_coding_worktree_diff("aliecs", "task1"))
            discarded = json.loads(mcp_main.discard_coding_worktree("aliecs", "task1"))

        self.assertEqual(created["task_id"], "task1")
        self.assertIn("diff --git", diff["output"])
        self.assertTrue(discarded["removed"])
        self.assertEqual(
            calls,
            [
                ("create", "aliecs", "task1", "HEAD"),
                ("diff", "aliecs", "task1", "HEAD"),
                ("discard", "aliecs", "task1"),
            ],
        )

    def test_create_worktree_unavailable_degrades_gracefully(self) -> None:
        def fake_create_worktree(repo, task_id, base_ref="HEAD"):
            raise mcp_main.executor_client.ExecutorUnavailable("no tunnel")

        with unittest.mock.patch.object(
            mcp_main.executor_client, "create_worktree", fake_create_worktree
        ):
            result = json.loads(mcp_main.create_coding_worktree("aliecs", "task1"))

        self.assertEqual(result["executor"], "unavailable")


if __name__ == "__main__":
    unittest.main()
