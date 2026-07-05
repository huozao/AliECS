"""配方域：配方查询、成本核算、BOM同步请求、查询结果导出下载与缓存预热。"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core import _conn, require_login, require_permission
from app.recipes.bom_query import calculate_recipe_costs, export_path_for_id, load_detail_from_workbook, locate_recipe_source, new_export_path, query_recipe_workbook, recipe_cost_export_filename, recipe_raw_export_filename, save_recipe_cost_workbook, save_recipe_human_workbook, save_recipe_workbook
from app.recipes.compare_export import compare_export_filename, save_compare_workbook
from app.recipes.price_lookup import latest_purchase_prices, latest_sales_prices


router = APIRouter()

class RecipeQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    default_bom: str = "all"
    include_disabled: bool = True


class RecipeCostRequest(RecipeQueryRequest):
    manual_prices: dict[str, float] = Field(default_factory=dict)
    simulated_quantities: dict[str, float] = Field(default_factory=dict)


class CompareCell(BaseModel):
    ratio: float | None = None
    qty: float | None = None
    delta: float | None = None
    is_new: bool = False


class CompareVersion(BaseModel):
    label: str = Field(max_length=200)
    code: str = Field(default="", max_length=100)
    version: str = Field(default="", max_length=100)
    is_base: bool = False
    is_target: bool = False


class CompareRow(BaseModel):
    status: str = Field(max_length=20)
    item_code: str = Field(max_length=100)
    item_name: str = Field(default="", max_length=200)
    spec: str = Field(default="", max_length=200)
    unit: str = Field(default="", max_length=40)
    code_warn: bool = False
    cells: list[CompareCell | None]


class CompareExportRequest(BaseModel):
    """对比表导出：前端把呈现态原样传来，后端只渲染 xlsx（对比逻辑唯一事实源在前端）。"""

    query: str = Field(default="", max_length=100)
    filter_label: str = Field(default="全部", max_length=20)
    versions: list[CompareVersion] = Field(min_length=1, max_length=60)
    rows: list[CompareRow] = Field(max_length=5000)
    view: dict[str, bool] = Field(default_factory=dict)


def _tplus_bom_sync_request_dir() -> Path:
    return Path(os.getenv("TPLUS_BOM_SYNC_REQUEST_DIR", "/tmp/aliecs-tplus-sync-requests"))


def _create_tplus_bom_sync_request(requested_by: str | None) -> dict[str, Any]:
    request_dir = _tplus_bom_sync_request_dir()
    request_dir.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    payload = {
        "id": request_id,
        "provider": "chanjet",
        "module": "bom",
        "mode": "manual_bom_full_include_disabled",
        "include_disabled": True,
        "requested_by": requested_by or "",
        "requested_at": int(time.time()),
    }
    (request_dir / f"{request_id}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {"id": request_id, "status": "pending", "mode": payload["mode"]}


# 查询结果导出文件延迟生成：查询时只记录上下文，用户真正点「下载原始明细」时才写 xlsx
# （save_recipe_workbook 约 2.3s，绝大多数查询并不会下载，没必要每次都写）。
_RECIPE_QUERY_CONTEXT: dict[str, dict[str, object]] = {}
_RECIPE_QUERY_CONTEXT_MAX = 256


def _remember_recipe_query(file_id: str, query: str, default_bom: str | None, include_disabled: bool) -> None:
    _RECIPE_QUERY_CONTEXT[file_id] = {
        "query": query,
        "default_bom": default_bom,
        "include_disabled": include_disabled,
    }
    while len(_RECIPE_QUERY_CONTEXT) > _RECIPE_QUERY_CONTEXT_MAX:
        _RECIPE_QUERY_CONTEXT.pop(next(iter(_RECIPE_QUERY_CONTEXT)))


def _latest_bom_sync_run() -> dict[str, Any] | None:
    """产出当前 BOM 文件的那次同步：最近一次成功且会导出 bom 的 run（scheduled_full 或手动 bom）。
    locate_recipe_source 取 mtime 最新文件 ⇔ 最近一次成功 bom 同步，故二者对应。任何异常降级为 None。"""
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, module, mode, status, finished_at
                    FROM integration_sync_runs
                    WHERE provider = 'chanjet' AND status = 'success' AND module IN ('all', 'bom')
                    ORDER BY finished_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "module": row[1],
                    "mode": row[2],
                    "status": row[3],
                    "finished_at": str(row[4]) if row[4] else None,
                }
    except Exception:
        return None


# ── 配方/价格缓存预热 ──────────────────────────────────────────────
# 冷解析在机器 CPU 突发时可达 9~17s（远超 nginx 60s）→ 宽查询 504 的根因之一。后台线程
# 定期探测（命中缓存只是一次 md5，极快），文件变化才在请求路径之外重解析，让用户请求永远
# 命中热缓存。任何异常都吞掉（数据未就绪时优雅跳过）。
_RECIPE_WARM_INTERVAL = max(15, int(os.getenv("RECIPE_CACHE_WARM_INTERVAL", "45")))


def warm_recipe_caches() -> None:
    try:
        load_detail_from_workbook(locate_recipe_source())
    except Exception:
        pass
    try:
        latest_purchase_prices()
        latest_sales_prices()
    except Exception:
        pass


def _recipe_cache_warm_loop() -> None:
    while True:
        warm_recipe_caches()
        time.sleep(_RECIPE_WARM_INTERVAL)


@router.on_event("startup")
def _start_recipe_cache_warmer() -> None:
    if os.getenv("RECIPE_CACHE_WARM", "1") != "1":
        return
    threading.Thread(target=_recipe_cache_warm_loop, name="recipe-cache-warmer", daemon=True).start()


@router.post("/v1/recipes/query")
def recipe_query(body: RecipeQueryRequest, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("formula.read", user)
    try:
        source_path = locate_recipe_source()
        result = query_recipe_workbook(
            source_path,
            query_text=body.query,
            default_bom=body.default_bom,
            include_disabled=body.include_disabled,
        )
        file_id, _output_path = new_export_path()
        _remember_recipe_query(file_id, body.query, body.default_bom, body.include_disabled)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="BOM 输入文件未找到") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"配方查询失败：{type(exc).__name__}") from exc

    return {
        "query": body.query,
        "source_file": source_path.name,
        "source_sync": _latest_bom_sync_run(),
        "match_count": result.match_count,
        "recipe_count": result.recipe_count,
        "default_bom": result.default_bom,
        "include_disabled": result.include_disabled,
        "file_id": file_id,
        "download_url": f"/v1/recipes/download/{file_id}",
        "preview": result.preview_rows(limit=5000),
    }


def _recipe_price_maps() -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    """最新采购/销售价格映射；任何读取异常都降级为空，不影响成本核算。"""
    try:
        return latest_purchase_prices(), latest_sales_prices()
    except Exception:
        return {}, {}


@router.post("/v1/recipes/cost")
def recipe_cost(body: RecipeCostRequest, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("formula.cost.calculate", user)
    try:
        source_path = locate_recipe_source()
        result = query_recipe_workbook(
            source_path,
            query_text=body.query,
            default_bom=body.default_bom,
            include_disabled=body.include_disabled,
        )
        purchase_prices, sales_prices = _recipe_price_maps()
        recipes = calculate_recipe_costs(
            result,
            manual_prices=body.manual_prices,
            simulated_quantities=body.simulated_quantities,
            purchase_prices=purchase_prices,
            sales_prices=sales_prices,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="BOM 输入文件未找到") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"配方成本核算失败：{type(exc).__name__}") from exc

    return {
        "query": body.query,
        "source_file": source_path.name,
        "recipe_count": len(recipes),
        "default_bom": result.default_bom,
        "include_disabled": result.include_disabled,
        "manual_price_count": len(body.manual_prices),
        "simulated_quantity_count": len(body.simulated_quantities),
        "recipes": recipes,
    }


@router.post("/v1/recipes/cost/export")
def recipe_cost_export(body: RecipeCostRequest, user: dict[str, Any] = Depends(require_login)) -> FileResponse:
    require_permission("formula.cost.calculate", user)
    try:
        source_path = locate_recipe_source()
        result = query_recipe_workbook(
            source_path,
            query_text=body.query,
            default_bom=body.default_bom,
            include_disabled=body.include_disabled,
        )
        purchase_prices, sales_prices = _recipe_price_maps()
        recipes = calculate_recipe_costs(
            result,
            manual_prices=body.manual_prices,
            simulated_quantities=body.simulated_quantities,
            purchase_prices=purchase_prices,
            sales_prices=sales_prices,
        )
        _file_id, output_path = new_export_path()
        save_recipe_cost_workbook(output_path, recipes)
        filename = recipe_cost_export_filename(recipes, body.query)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="BOM 输入文件未找到") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"配方成本核算导出失败：{type(exc).__name__}") from exc

    return FileResponse(
        output_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )



@router.post("/v1/recipes/compare/export")
def recipe_compare_export(body: CompareExportRequest, user: dict[str, Any] = Depends(require_login)) -> FileResponse:
    require_permission("formula.read", user)
    for row in body.rows:
        if len(row.cells) != len(body.versions):
            raise HTTPException(status_code=400, detail="行的单元格数量与版本列数不一致。")
    try:
        _file_id, output_path = new_export_path()
        save_compare_workbook(output_path, body.model_dump())
        filename = compare_export_filename(body.query)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"对比表导出失败：{type(exc).__name__}") from exc
    return FileResponse(
        output_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


@router.post("/v1/recipes/sync-bom")
def recipe_sync_bom(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("formula.read", user)
    try:
        request = _create_tplus_bom_sync_request(user.get("sub"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"BOM 同步请求创建失败：{type(exc).__name__}") from exc
    return {
        **request,
        "module": "bom",
        "include_disabled": True,
        "message": "已请求同步 T+ 物料清单 BOM；默认全量包含停用配方。",
    }


@router.get("/v1/recipes/download/{file_id}")
def recipe_download(file_id: str, sheet: str | None = None, user: dict[str, Any] = Depends(require_login)) -> FileResponse:
    require_permission("formula.read", user)
    if sheet not in (None, "", "human"):
        raise HTTPException(status_code=400, detail="不支持的 sheet 参数，目前仅支持 human。")
    human_only = sheet == "human"
    try:
        path = export_path_for_id(file_id, variant="human" if human_only else "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    context = _RECIPE_QUERY_CONTEXT.get(file_id)
    if not path.is_file():
        # 延迟生成：用查询时记录的上下文按需写出导出文件
        if context is None:
            raise HTTPException(status_code=404, detail="下载文件已过期，请重新查询后再下载。")
        try:
            result = query_recipe_workbook(
                locate_recipe_source(),
                query_text=str(context["query"]),
                default_bom=context["default_bom"],
                include_disabled=bool(context["include_disabled"]),
            )
            if human_only:
                save_recipe_human_workbook(path, result)
            else:
                save_recipe_workbook(path, result)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"导出文件生成失败：{type(exc).__name__}") from exc
    filename = recipe_raw_export_filename(str(context["query"]) if context else None, human_only=human_only)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
