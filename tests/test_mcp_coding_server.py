from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "services" / "mcp-coding-server"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from app import main as mcp_main  # noqa: E402


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
