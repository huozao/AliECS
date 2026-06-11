from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "services" / "mcp-coding-server" / "app" / "main.py"

# 不能用 `from app import main`：CI 与 backend-api 测试共享一次 unittest
# discover，`app` 包名已被 backend-api 占用，必须按文件路径独立加载。
_spec = importlib.util.spec_from_file_location("mcp_coding_server_main", MAIN_PATH)
assert _spec and _spec.loader
mcp_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_main)


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
        self.assertIn("start_coding_task", payload["planned_tools"])

    def test_only_read_only_tools_registered(self) -> None:
        tools = asyncio.run(mcp_main.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(names, {"ping", "server_info"})
        for tool in tools:
            self.assertIsNotNone(tool.annotations, f"{tool.name} 缺少 annotations")
            self.assertTrue(
                tool.annotations.readOnlyHint,
                f"{tool.name} 必须声明 readOnlyHint（阶段一只允许只读工具）",
            )


if __name__ == "__main__":
    unittest.main()
