"""Clash 配置合成：第三方机场订阅源的增删改，以及合成配置的预览与下载。

自建节点定义来自环境变量 CLASH_SELF_NODES_B64（由 SOPS 管理、部署时渲染），
仓库里没有也不得有。机场订阅 URL 存库，不进仓库。

本模块不访问机场——机场节点由客户端 mihomo 通过 proxy-providers 自行拉取，
因此这里没有 HTTP 客户端、没有定时任务、没有快照表。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from contextlib import closing
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.clash_profile.render import render_profile
from app.core import _conn, require_admin


router = APIRouter(prefix="/v1/admin/clash-profile", tags=["clash-profile"])

_COLUMNS = "id, name, url, enabled, sort_order"


class ProviderIn(BaseModel):
    name: str
    url: str
    enabled: bool = True
    sort_order: int = 0


def _row_to_dict(row: tuple) -> dict[str, Any]:
    return {"id": row[0], "name": row[1], "url": row[2], "enabled": row[3], "sort_order": row[4]}


def _load_self_nodes() -> list[dict[str, Any]]:
    """从 CLASH_SELF_NODES_B64 读自建节点定义。

    ⚠️ 值是 base64 而不是裸 JSON，这一点不能"简化"回去。runtime env 会被
    `deploy/ecs/deploy.sh` 以 `set -a; source <file>` 载入，bash 的引号移除会把
    `[{"name":"a"}]` 吃成 `[{name:a}]`（2026-08-11 实测），json.loads 随后报错。
    整条链路是 bash source → heredoc 展开 → dotenv → compose 插值四层，每层的引号
    语义都不一样；base64 的字符集穿这四层都不需要任何转义。
    """
    raw = os.getenv("CLASH_SELF_NODES_B64", "").strip()
    if not raw:
        raise HTTPException(status_code=500, detail="CLASH_SELF_NODES_B64 未配置，无法生成配置")
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=500, detail=f"CLASH_SELF_NODES_B64 不是合法 base64/UTF-8：{exc}"
        ) from exc
    try:
        nodes = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500, detail=f"CLASH_SELF_NODES_B64 解码后不是合法 JSON：{exc}"
        ) from exc
    if not isinstance(nodes, list) or not nodes:
        raise HTTPException(status_code=500, detail="CLASH_SELF_NODES_B64 解码后必须是非空数组")
    for node in nodes:
        if not isinstance(node, dict) or "name" not in node or "server" not in node:
            raise HTTPException(
                status_code=500,
                detail="CLASH_SELF_NODES_B64 的每个元素都必须是含 name 与 server 的对象",
            )
    return nodes


def _rows() -> list[dict[str, Any]]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM clash_profile_providers ORDER BY sort_order, id")
            return [_row_to_dict(row) for row in cur.fetchall()]


def _profile_text() -> str:
    try:
        return render_profile(_load_self_nodes(), _rows())
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/providers")
def list_providers(_: dict = Depends(require_admin)) -> dict[str, Any]:
    return {"items": _rows()}


@router.post("/providers", status_code=201)
def create_provider(payload: ProviderIn, _: dict = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clash_profile_providers(name, url, enabled, sort_order)"
                f" VALUES (%s, %s, %s, %s) RETURNING {_COLUMNS}",
                (payload.name, payload.url, payload.enabled, payload.sort_order),
            )
            row = cur.fetchone()
        conn.commit()
    return _row_to_dict(row)


@router.put("/providers/{provider_id}")
def update_provider(
    provider_id: int, payload: ProviderIn, _: dict = Depends(require_admin)
) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE clash_profile_providers"
                " SET name = %s, url = %s, enabled = %s, sort_order = %s, updated_at = now()"
                f" WHERE id = %s RETURNING {_COLUMNS}",
                (payload.name, payload.url, payload.enabled, payload.sort_order, provider_id),
            )
            row = cur.fetchone()
        conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="订阅源不存在")
    return _row_to_dict(row)


# 与 couple.py 等既有 DELETE 端点保持一致：返回 200 + dict，不用 204。
# 用 204 会踩 FastAPI 的坑——它从返回标注推断 response_model，而 204 不允许响应体。
@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: int, _: dict = Depends(require_admin)) -> dict[str, str]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM clash_profile_providers WHERE id = %s", (provider_id,))
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="订阅源不存在")
    return {"status": "deleted"}


@router.get("/preview", response_class=PlainTextResponse)
def preview_profile(_: dict = Depends(require_admin)) -> PlainTextResponse:
    return PlainTextResponse(_profile_text(), media_type="text/plain; charset=utf-8")


@router.get("/download", response_class=PlainTextResponse)
def download_profile(_: dict = Depends(require_admin)) -> PlainTextResponse:
    return PlainTextResponse(
        _profile_text(),
        media_type="text/yaml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="clash-profile.yaml"'},
    )
