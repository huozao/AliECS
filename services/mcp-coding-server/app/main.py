"""MCP coding bridge for ChatGPT developer-mode connectors.

Phase 2: read-only connectivity tools (ping / server_info) plus dry-run coding
tools that proxy to the 开发机 executor through a reverse SSH tunnel. The
executor only performs read-only git actions in this phase; mutating actions
arrive later behind explicit confirmation.

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

from . import executor_client

SERVER_NAME = "aliecs-coding"
SERVER_VERSION = "0.2.0"
PHASE = "phase-2-dryrun"
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
        "executor_configured": executor_client.is_configured(),
        "tools": [
            "ping",
            "server_info",
            "list_coding_targets",
            "start_coding_task",
            "get_coding_task",
        ],
        "note": "阶段二：编程任务仅支持只读 git 操作（dry-run）。",
    }


mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "AliECS 编程桥接服务，阶段二（dry-run）。\n"
        "- ping / server_info：连通性与状态，只读。\n"
        "- list_coding_targets：列出可操作的仓库白名单与允许的只读操作。\n"
        "- start_coding_task：在开发机对某仓库发起一个只读 git 任务（git_status / "
        "git_log / git_diff / list_files / read_file），返回任务 id。\n"
        "- get_coding_task：用任务 id 轮询状态与结果。\n"
        "本阶段不会修改任何文件；遇到需要写入的请求请直接拒绝并说明仍在 dry-run 阶段。"
    ),
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8090")),
    stateless_http=True,
    json_response=True,
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# start_coding_task reaches the dev machine and creates a job, so it is not a
# read-only hint even though phase-2 actions themselves don't mutate anything.
# This makes ChatGPT show the write-confirmation modal on task start.
TASK_START = ToolAnnotations(readOnlyHint=False, openWorldHint=True)


@mcp.tool(annotations=READ_ONLY)
def ping(message: str = "ping") -> str:
    """连通性检查：原样回显 message，并附带服务器 UTC 时间与服务版本。只读，无副作用。"""
    return json.dumps(ping_payload(message), ensure_ascii=False)


@mcp.tool(annotations=READ_ONLY)
def server_info() -> str:
    """返回编程桥接服务的版本、所处阶段、运行时长与可用工具。只读，无副作用。"""
    return json.dumps(server_info_payload(), ensure_ascii=False)


def _unavailable(detail: str) -> str:
    return json.dumps(
        {
            "executor": "unavailable",
            "detail": detail,
            "hint": "开发机 executor 或反向隧道可能未启动；这是预期内的优雅降级，不要重试很多次。",
        },
        ensure_ascii=False,
    )


@mcp.tool(annotations=READ_ONLY)
def list_coding_targets() -> str:
    """列出开发机上允许操作的仓库白名单与本阶段允许的只读操作。只读。"""
    try:
        return json.dumps(executor_client.list_targets(), ensure_ascii=False)
    except executor_client.ExecutorUnavailable as exc:
        return _unavailable(str(exc))


@mcp.tool(annotations=TASK_START)
def start_coding_task(repo: str, action: str, params: dict | None = None) -> str:
    """在开发机对指定仓库发起一个只读 git 任务，返回任务 id 供后续轮询。

    repo：list_coding_targets 返回的仓库名。
    action：git_status / git_log / git_diff / list_files / read_file 之一。
    params：可选参数，如 {"count": 20}、{"ref": "HEAD~1"}、{"path": "README.md"}。
    本阶段为 dry-run，只读取不修改。
    """
    try:
        return json.dumps(
            executor_client.create_task(repo, action, params), ensure_ascii=False
        )
    except executor_client.ExecutorUnavailable as exc:
        return _unavailable(str(exc))


@mcp.tool(annotations=READ_ONLY)
def get_coding_task(task_id: str) -> str:
    """用 start_coding_task 返回的 id 查询任务状态与结果。只读。"""
    try:
        return json.dumps(executor_client.get_task(task_id), ensure_ascii=False)
    except executor_client.ExecutorUnavailable as exc:
        return _unavailable(str(exc))


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "service": "mcp-coding-server", "version": SERVER_VERSION}
    )


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
