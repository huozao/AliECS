from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
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
            },
        )

    def test_readonly_annotations_are_correct(self) -> None:
        tools = {t.name: t for t in asyncio.run(mcp_main.mcp.list_tools())}
        for name in ("ping", "server_info", "list_coding_targets", "get_coding_task"):
            self.assertTrue(tools[name].annotations.readOnlyHint, f"{name} 应为只读")
        # Task start reaches the dev machine and creates a job: not read-only,
        # so ChatGPT surfaces a confirmation modal.
        self.assertFalse(tools["start_coding_task"].annotations.readOnlyHint)

    def test_tools_degrade_gracefully_without_executor(self) -> None:
        import os

        os.environ.pop("EXECUTOR_BASE_URL", None)
        os.environ.pop("EXECUTOR_TOKEN", None)
        self.assertFalse(mcp_main.executor_client.is_configured())


if __name__ == "__main__":
    unittest.main()
