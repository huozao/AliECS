"""MCP coding bridge for ChatGPT developer-mode connectors.

Phase 1 (PoC): read-only connectivity tools only. Coding task tools
(start_coding_task / get_task_status / get_task_result) arrive in later
phases and will proxy to the dev-machine executor through a reverse tunnel.

The container listens on MCP_PORT (default 8090) and is published only on
127.0.0.1; public exposure happens via an Nginx location with a secret path
that lives in ECS runtime config, never in this repository.
"""

from __future__ import annotations

import json
import os
import platform
import time
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

SERVER_NAME = "aliecs-coding"
SERVER_VERSION = "0.1.0"
PHASE = "poc-1-readonly"
STARTED_AT = time.monotonic()


def ping_payload(message: str) -> dict:
    return {
        "echo": message,
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "service": SERVER_NAME,
        "version": SERVER_VERSION,
    }


def server_info_payload() -> dict:
    return {
        "service": SERVER_NAME,
        "version": SERVER_VERSION,
        "phase": PHASE,
        "uptime_seconds": round(time.monotonic() - STARTED_AT, 1),
        "python": platform.python_version(),
        "planned_tools": [
            "start_coding_task",
            "get_task_status",
            "get_task_result",
        ],
    }


mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "AliECS 编程桥接服务，阶段一（连通性 PoC）。当前所有工具均为只读；"
        "用 ping 验证往返链路，用 server_info 查看版本与后续规划。"
    ),
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8090")),
    stateless_http=True,
    json_response=True,
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@mcp.tool(annotations=READ_ONLY)
def ping(message: str = "ping") -> str:
    """连通性检查：原样回显 message，并附带服务器 UTC 时间与服务版本。只读，无副作用。"""
    return json.dumps(ping_payload(message), ensure_ascii=False)


@mcp.tool(annotations=READ_ONLY)
def server_info() -> str:
    """返回编程桥接服务的版本、所处阶段、运行时长与后续规划的工具列表。只读，无副作用。"""
    return json.dumps(server_info_payload(), ensure_ascii=False)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "service": "mcp-coding-server", "version": SERVER_VERSION}
    )


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
