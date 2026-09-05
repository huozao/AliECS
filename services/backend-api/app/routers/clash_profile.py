"""Clash 配置合成：第三方机场订阅源的增删改，以及合成配置的预览与下载。

自建节点定义来自环境变量 CLASH_SELF_NODES_B64（由 SOPS 管理、部署时渲染），
仓库里没有也不得有。机场订阅 URL 存库，不进仓库。

⚠️ 2026-08-15 起本模块**会访问机场**。原先的注释是"不访问机场，机场节点由客户端
mihomo 自行拉取，因此没有 HTTP 客户端、没有快照表"——该写法自 2026-08-15 起失效：
机场按源 IP 封了家宽，客户端拉不到（详见 clash_profile/fetch.py 开头）。
拉取实现在 `clash_profile/fetch.py`，落库在 `clash_profile/store.py`。

仍然没有定时任务：调度放在消费端（本机每日计划任务经 SSH + docker exec 调
`clash_profile/cli.py`），服务端不新增调度设施、不新增部署单元。
"""

from __future__ import annotations

from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.clash_profile import store
from app.clash_profile.fetch import fetch_snapshot
from app.clash_profile.render import load_self_nodes, provider_key, render_profile
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
    try:
        return load_self_nodes()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _rows() -> list[dict[str, Any]]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM clash_profile_providers ORDER BY sort_order, id")
            return [_row_to_dict(row) for row in cur.fetchall()]


def _profile_text(target: Literal["desktop", "webdock", "mobile"] = "desktop") -> str:
    try:
        providers = _rows()
        contents = None
        if target == "mobile":
            contents = {
                provider["id"]: store.read_content(provider["id"])
                for provider in providers
                if provider["enabled"]
            }
        return render_profile(_load_self_nodes(), providers, target=target, provider_contents=contents)
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
def preview_profile(
    target: Literal["desktop", "webdock", "mobile"] = "desktop",
    _: dict = Depends(require_admin),
) -> PlainTextResponse:
    return PlainTextResponse(_profile_text(target), media_type="text/plain; charset=utf-8")


@router.get("/download", response_class=PlainTextResponse)
def download_profile(
    target: Literal["desktop", "webdock", "mobile"] = "desktop",
    _: dict = Depends(require_admin),
) -> PlainTextResponse:
    return PlainTextResponse(
        _profile_text(target),
        media_type="text/yaml; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="clash-profile-{target}.yaml"'
                if target != "desktop"
                else 'attachment; filename="clash-profile.yaml"'
            )
        },
    )


@router.get("/snapshots")
def list_snapshots(_: dict = Depends(require_admin)) -> dict[str, Any]:
    """各订阅源的拉取状态。不含节点正文——状态接口不该把节点凭据带进响应。"""
    status = store.read_status()
    items = []
    for provider in _rows():
        snapshot = status.get(provider["id"], {})
        items.append({
            "provider_id": provider["id"],
            "key": provider_key(provider["id"]),
            "name": provider["name"],
            "enabled": provider["enabled"],
            "node_count": snapshot.get("node_count", 0),
            "fingerprint": snapshot.get("fingerprint", ""),
            "userinfo": snapshot.get("userinfo", ""),
            "fetched_at": snapshot.get("fetched_at"),
            "changed_at": snapshot.get("changed_at"),
            "last_error": snapshot.get("last_error", ""),
            "last_error_at": snapshot.get("last_error_at"),
        })
    return {"items": items}


@router.post("/providers/{provider_id}/fetch")
def fetch_provider_now(provider_id: int, _: dict = Depends(require_admin)) -> dict[str, Any]:
    """后台「立即拉取」。改完订阅 URL 后不必等本机的下一轮定时同步。"""
    provider = next((p for p in _rows() if p["id"] == provider_id), None)
    if provider is None:
        raise HTTPException(status_code=404, detail="订阅源不存在")
    try:
        snapshot = fetch_snapshot(provider["url"])
    except RuntimeError as exc:
        store.save_error(provider_id, str(exc))
        # 502 而不是 500：失败方是上游机场，不是本服务。
        raise HTTPException(status_code=502, detail=f"拉取失败：{exc}") from exc
    changed = store.save_snapshot(provider_id, snapshot)
    return {
        "status": "ok",
        "node_count": snapshot.node_count,
        "fingerprint": snapshot.fingerprint,
        "changed": changed,
    }


@router.get("/nodes/{provider_id}", response_class=PlainTextResponse)
def download_nodes(provider_id: int, _: dict = Depends(require_admin)) -> PlainTextResponse:
    """节点文件。产物里的 proxy-provider 是 type: file，客户端要把它放到 providers/ 下。"""
    content = store.read_content(provider_id)
    if not content:
        raise HTTPException(status_code=404, detail="该订阅源还没有可用快照，请先拉取")
    filename = f"{provider_key(provider_id)}.yaml"
    return PlainTextResponse(
        content,
        media_type="text/yaml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
