"""MCP coding bridge for ChatGPT developer-mode connectors.

Phase 3a: connectivity tools plus git worktree-isolated write tools that proxy
to the 开发机 executor through a reverse SSH tunnel. Mutating actions must run
inside a dedicated codex-task-<task_id> worktree branch.

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
from .oauth.config import config_from_env as _oauth_config_from_env
from .oauth.consent import make_consent_handler as _make_consent_handler
from .oauth.provider import AliecsOAuthProvider as _AliecsOAuthProvider
from .oauth.store import OAuthStore as _OAuthStore

SERVER_NAME = "aliecs-coding"
SERVER_VERSION = "0.3.0"
PHASE = "phase-4-oauth"
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
            "create_coding_worktree",
            "discard_coding_worktree",
            "get_coding_worktree_diff",
        ],
        "note": (
            "阶段四：连接器需 OAuth 鉴权；阶段三 a：只读 git 操作仍是 dry-run；"
            "写操作（write_file / apply_patch / "
            "git_commit）必须先用 create_coding_worktree 创建隔离 worktree，"
            "在该 worktree 分支上进行，绝不直接修改主工作区，也不会自动 push/merge。"
        ),
    }


_OAUTH_CONFIG = _oauth_config_from_env()
_oauth_kwargs: dict = {}
_oauth_provider = None
if _OAUTH_CONFIG.enabled:
    if not _OAUTH_CONFIG.fully_configured:
        raise RuntimeError(
            "MCP_OAUTH_ENABLED=true 但缺少 MCP_OAUTH_ISSUER/PASSPHRASE/SIGNING_SECRET/STORE_PATH"
        )
    from mcp.server.auth.settings import (
        AuthSettings,
        ClientRegistrationOptions,
        RevocationOptions,
    )

    _oauth_store = _OAuthStore(_OAUTH_CONFIG.store_path, _OAUTH_CONFIG.pepper)
    _oauth_provider = _AliecsOAuthProvider(_OAUTH_CONFIG, _oauth_store)
    _oauth_kwargs = dict(
        auth_server_provider=_oauth_provider,
        auth=AuthSettings(
            issuer_url=_OAUTH_CONFIG.issuer_url,
            resource_server_url=_OAUTH_CONFIG.issuer_url,
            required_scopes=[_OAUTH_CONFIG.scope],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[_OAUTH_CONFIG.scope],
                default_scopes=[_OAUTH_CONFIG.scope],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )


mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "AliECS 编程桥接服务，阶段四（OAuth 鉴权 + worktree 隔离写入）。\n"
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
    **_oauth_kwargs,
)

if _oauth_provider is not None:
    mcp.custom_route("/oauth/consent", methods=["GET", "POST"])(
        _make_consent_handler(_oauth_provider, _OAUTH_CONFIG)
    )

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Task-start tools reach the dev machine or mutate an isolated worktree, so they
# should surface ChatGPT's confirmation modal.
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
    """列出开发机上允许操作的仓库白名单、本阶段允许的只读操作与写操作。只读。"""
    try:
        return json.dumps(executor_client.list_targets(), ensure_ascii=False)
    except executor_client.ExecutorUnavailable as exc:
        return _unavailable(str(exc))


@mcp.tool(annotations=TASK_START)
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


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "service": "mcp-coding-server", "version": SERVER_VERSION}
    )


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
