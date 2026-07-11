"""T+ BOM 草稿、校验、提交和状态 API。"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request

from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.core import _audit, _conn, require_login, require_permission
from app.routers.exports import _inventory_scope_config, _latest_tplus_export_file
from app.tplus_bom import build_bom_create_payload, build_custom_inventory_requests, validate_bom_draft


router = APIRouter(prefix="/v1/tplus", tags=["tplus-bom"])


class BomParent(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(default="", max_length=200)
    specification: str = Field(default="", max_length=200)
    unit_name: str = Field(min_length=1, max_length=40)
    unit_code: str = Field(default="", max_length=40)
    source: Literal["tplus", "custom"] = "tplus"
    inventory_class_code: str = Field(default="", max_length=100)
    inventory_class_name: str = Field(default="", max_length=200)


class BomChild(BomParent):
    required_quantity: str = Field(min_length=1, max_length=40)
    warehouse_code: str = Field(default="", max_length=100)
    child_bom_version: str = Field(default="", max_length=100)


class BomOptions(BaseModel):
    version: str = Field(min_length=1, max_length=100)
    produce_quantity: str = Field(default="1", max_length=40)
    yield_rate: str = Field(default="1", max_length=40)
    is_default_bom: bool = False
    warehouse_code: str = Field(default="", max_length=100)
    routing_code: str = Field(default="", max_length=100)
    manufacture_plant_code: str = Field(default="", max_length=100)


class BomDraftBody(BaseModel):
    parent: BomParent
    children: list[BomChild] = Field(min_length=1, max_length=500)
    options: BomOptions


class BomSubmitBody(BaseModel):
    confirmed: bool = False


def _actor(user: dict[str, Any]) -> str:
    return str(user.get("username") or user.get("sub") or "").strip()


def _require_bom_write(user: dict[str, Any]) -> None:
    require_permission("tplus.bom.write", user)


def _write_enabled() -> bool:
    return os.getenv("TPLUS_BOM_WRITE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _draft_response(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0], "status": row[1], "parent": row[2], "children": row[3], "options": row[4],
        "created_by": row[5], "created_at": row[6], "updated_at": row[7], "submitted_at": row[8],
    }


def _load_owned_draft(conn, draft_id: int, actor: str, is_admin: bool) -> tuple[Any, ...]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, status, parent_json, children_json, options_json, created_by,
                      created_at, updated_at, submitted_at
               FROM tplus_bom_drafts WHERE id = %s""",
            (draft_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="BOM 草稿不存在")
    if not is_admin and str(row[5]) != actor:
        raise HTTPException(status_code=403, detail="permission denied")
    return row


def _inventory_column(df, *names: str) -> str:
    for name in names:
        if name in df.columns:
            return name
    return ""


def _chanjet_open_token() -> str:
    token_file = os.getenv("CHANJET_OPEN_TOKEN_FILE", "").strip()
    if token_file:
        try:
            token = open(token_file, "r", encoding="utf-8").read().strip()
            if token:
                return token
        except OSError:
            pass
    return os.getenv("CHANJET_OPEN_TOKEN", "").strip()


def _chanjet_read_post(endpoint: str, payload: dict[str, Any]) -> Any:
    app_key = os.getenv("CHANJET_APP_KEY", "").strip()
    app_secret = os.getenv("CHANJET_APP_SECRET", "").strip()
    open_token = _chanjet_open_token()
    if not app_key or not app_secret or not open_token:
        raise HTTPException(status_code=503, detail="T+ 查询凭据未配置")
    base_url = os.getenv("CHANJET_BASE_URL", "https://openapi.chanjet.com").rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/{endpoint.lstrip('/')}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "appKey": app_key,
            "appSecret": app_secret,
            "openToken": open_token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"T+ 分类查询失败：{type(exc).__name__}") from exc


@router.get("/inventory-create-options")
def tplus_inventory_create_options(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    _require_bom_write(user)
    path = _latest_tplus_export_file("inventory")
    if path is None:
        raise HTTPException(status_code=404, detail="存货档案尚未同步")
    import pandas as pd

    df = pd.read_excel(path, dtype=str).fillna("")
    unit_code_col = _inventory_column(df, "BaseUnitCode", "Unit.Code", "UnitCode", "计量单位编码")
    unit_name_col = _inventory_column(df, "BaseUnitName", "Unit.Name", "UnitName", "计量单位", "单位")
    if not unit_code_col or not unit_name_col:
        raise HTTPException(status_code=409, detail="存货档案缺少计量单位字段")
    units = sorted(
        {
            (str(row[unit_code_col]).strip(), str(row[unit_name_col]).strip())
            for _, row in df.iterrows()
            if str(row[unit_code_col]).strip() and str(row[unit_name_col]).strip()
        }
    )
    class_response = _chanjet_read_post(
        "/tplus/api/v2/inventoryClass/Query",
        {"param": {"SelectFields": "Code,Name,IsEndNode"}},
    )
    class_rows = class_response if isinstance(class_response, list) else []
    classes = [
        {"code": str(row.get("Code") or row.get("code") or "").strip(), "name": str(row.get("Name") or row.get("name") or "").strip()}
        for row in class_rows
        if isinstance(row, dict) and bool(row.get("IsEndNode", row.get("isendnode", True)))
    ]
    classes = [item for item in classes if item["code"] and item["name"]]
    return {
        "classes": classes,
        "units": [{"code": code, "name": name} for code, name in units],
        "source_file": path.name,
    }


@router.get("/inventories")
def tplus_inventory_choices(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    scope: Literal["all", "material"] = Query(default="all"),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    _require_bom_write(user)
    path = _latest_tplus_export_file("inventory")
    if path is None:
        raise HTTPException(status_code=404, detail="存货档案尚未同步")

    import pandas as pd

    df = pd.read_excel(path, dtype=str).fillna("")
    code_col = _inventory_column(df, "Code", "InventoryCode", "存货编码")
    name_col = _inventory_column(df, "Name", "InventoryName", "存货名称")
    unit_col = _inventory_column(df, "BaseUnitName", "Unit.Name", "UnitName", "计量单位", "单位")
    unit_code_col = _inventory_column(df, "BaseUnitCode", "Unit.Code", "UnitCode", "计量单位编码")
    class_code_col = _inventory_column(df, "InventoryClass.Code", "InventoryClassCode", "存货分类编码")
    class_name_col = _inventory_column(df, "InventoryClass.Name", "InventoryClassName", "存货分类")
    spec_col = _inventory_column(df, "Specification", "规格型号")
    disabled_col = _inventory_column(df, "Disabled", "停用")
    if not code_col or not name_col or not unit_col:
        raise HTTPException(status_code=409, detail="存货档案缺少编码、名称或计量单位字段")
    if disabled_col:
        disabled = df[disabled_col].astype(str).str.strip().str.lower()
        df = df[~disabled.isin({"1", "true", "yes", "是"})]
    stock_by_code: dict[str, dict[str, Any]] = {}
    if scope == "material":
        stock_path = _latest_tplus_export_file("current_stock")
        if stock_path is None:
            raise HTTPException(status_code=404, detail="原材料库存尚未同步")
        stock = pd.read_excel(stock_path, dtype=str).fillna("")
        required = {"InventoryCode", "WarehouseCode"}
        if not required.issubset(set(stock.columns)):
            raise HTTPException(status_code=409, detail="原材料库存缺少存货或仓库字段")
        raw_warehouses, _ = _inventory_scope_config()
        stock = stock[stock["WarehouseCode"].astype(str).str.strip().isin(raw_warehouses)]
        for _, item in stock.iterrows():
            code = str(item.get("InventoryCode") or "").strip()
            if not code:
                continue
            current = stock_by_code.setdefault(code, {"existing_quantity": 0.0, "available_quantity": 0.0})
            existing_qty = pd.to_numeric(item.get("ExistingQuantity"), errors="coerce")
            available_qty = pd.to_numeric(item.get("AvailableQuantity"), errors="coerce")
            current["existing_quantity"] += 0.0 if pd.isna(existing_qty) else float(existing_qty)
            current["available_quantity"] += 0.0 if pd.isna(available_qty) else float(available_qty)
        df = df[df[code_col].astype(str).str.strip().isin(stock_by_code)]
    keyword = q.strip().lower()
    if keyword:
        mask = df[code_col].str.lower().str.contains(keyword, regex=False) | df[name_col].str.lower().str.contains(keyword, regex=False)
        if spec_col:
            mask |= df[spec_col].str.lower().str.contains(keyword, regex=False)
        df = df[mask]
    df = df.drop_duplicates(subset=[code_col]).head(limit)
    items = [
        {
            "code": str(row[code_col]).strip(),
            "name": str(row[name_col]).strip(),
            "specification": str(row[spec_col]).strip() if spec_col else "",
            "unit_name": str(row[unit_col]).strip(),
            "unit_code": str(row[unit_code_col]).strip() if unit_code_col else "",
            "inventory_class_code": str(row[class_code_col]).strip() if class_code_col else "",
            "inventory_class_name": str(row[class_name_col]).strip() if class_name_col else "",
            "source": "tplus",
            **stock_by_code.get(str(row[code_col]).strip(), {}),
        }
        for _, row in df.iterrows()
        if str(row[code_col]).strip() and str(row[unit_col]).strip()
    ]
    stat = path.stat()
    return {
        "items": items, "total": len(items), "source_file": path.name,
        "synced_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


@router.post("/bom-drafts", status_code=201)
def create_bom_draft(body: BomDraftBody, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    _require_bom_write(user)
    actor = _actor(user)
    payload = body.model_dump()
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO tplus_bom_drafts(parent_json, children_json, options_json, created_by)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (Jsonb(payload["parent"]), Jsonb(payload["children"]), Jsonb(payload["options"]), actor),
            )
            draft_id = int(cur.fetchone()[0])
        conn.commit()
    _audit(actor, "tplus.bom_draft.create", "tplus_bom_drafts", str(draft_id))
    return {"id": draft_id, "status": "draft"}


@router.get("/bom-drafts/{draft_id}")
def get_bom_draft(draft_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    _require_bom_write(user)
    actor = _actor(user)
    is_admin = "admin" in user.get("roles", []) or "admin.access" in user.get("permissions", [])
    with closing(_conn()) as conn:
        return _draft_response(_load_owned_draft(conn, draft_id, actor, is_admin))


@router.patch("/bom-drafts/{draft_id}")
def update_bom_draft(draft_id: int, body: BomDraftBody, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    _require_bom_write(user)
    actor = _actor(user)
    is_admin = "admin" in user.get("roles", []) or "admin.access" in user.get("permissions", [])
    payload = body.model_dump()
    with closing(_conn()) as conn:
        row = _load_owned_draft(conn, draft_id, actor, is_admin)
        if row[1] != "draft":
            raise HTTPException(status_code=409, detail="已提交的草稿不能修改")
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE tplus_bom_drafts SET parent_json=%s, children_json=%s, options_json=%s, updated_at=NOW()
                   WHERE id=%s""",
                (Jsonb(payload["parent"]), Jsonb(payload["children"]), Jsonb(payload["options"]), draft_id),
            )
        conn.commit()
    _audit(actor, "tplus.bom_draft.update", "tplus_bom_drafts", str(draft_id))
    return {"id": draft_id, "status": "draft"}


@router.post("/bom-drafts/{draft_id}/validate")
def validate_draft(draft_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    _require_bom_write(user)
    actor = _actor(user)
    is_admin = "admin" in user.get("roles", []) or "admin.access" in user.get("permissions", [])
    with closing(_conn()) as conn:
        row = _load_owned_draft(conn, draft_id, actor, is_admin)
    errors = validate_bom_draft(row[2], row[3], row[4])
    return {"valid": not errors, "errors": errors, "payload_preview": build_bom_create_payload(row[2], row[3], row[4]) if not errors else None}


@router.post("/bom-drafts/{draft_id}/submit", status_code=202)
def submit_bom_draft(
    draft_id: int,
    body: BomSubmitBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    _require_bom_write(user)
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="必须明确确认后才能写入 T+")
    if not _write_enabled():
        raise HTTPException(status_code=503, detail="T+ BOM 写入功能尚未启用")
    actor = _actor(user)
    is_admin = "admin" in user.get("roles", []) or "admin.access" in user.get("permissions", [])
    with closing(_conn()) as conn:
        row = _load_owned_draft(conn, draft_id, actor, is_admin)
        payload = build_bom_create_payload(row[2], row[3], row[4])
        request_envelope = {
            "bom": payload,
            "custom_inventories": build_custom_inventory_requests(row[2], row[3]),
        }
        stable_key = (idempotency_key or f"bom-draft-{draft_id}").strip()
        if len(stable_key) > 200:
            raise HTTPException(status_code=400, detail="Idempotency-Key 过长")
        digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()
        with conn.cursor() as cur:
            cur.execute("SELECT id, status FROM tplus_bom_submissions WHERE draft_id=%s OR idempotency_key=%s", (draft_id, digest))
            existing = cur.fetchone()
            if existing:
                return {"id": existing[0], "draft_id": draft_id, "status": existing[1], "deduplicated": True}
            cur.execute(
                """INSERT INTO tplus_bom_submissions(draft_id, idempotency_key, requested_by, request_json)
                   VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                (draft_id, digest, actor, Jsonb(request_envelope)),
            )
            inserted = cur.fetchone()
            if not inserted:
                cur.execute(
                    "SELECT id, status FROM tplus_bom_submissions WHERE draft_id=%s OR idempotency_key=%s",
                    (draft_id, digest),
                )
                concurrent = cur.fetchone()
                if concurrent:
                    return {"id": concurrent[0], "draft_id": draft_id, "status": concurrent[1], "deduplicated": True}
                raise HTTPException(status_code=409, detail="BOM 提交幂等键冲突")
            submission_id = int(inserted[0])
            cur.execute("UPDATE tplus_bom_drafts SET status='submitted', submitted_at=NOW(), updated_at=NOW() WHERE id=%s", (draft_id,))
            cur.execute(
                "INSERT INTO tplus_bom_submission_events(submission_id, event_type, detail_json) VALUES (%s, 'queued', %s)",
                (submission_id, Jsonb({"requested_by": actor})),
            )
        conn.commit()
    _audit(actor, "tplus.bom.submit", "tplus_bom_submissions", str(submission_id), {"draft_id": draft_id})
    return {"id": submission_id, "draft_id": draft_id, "status": "pending", "deduplicated": False}


@router.get("/bom-submissions/{submission_id}")
def get_bom_submission(submission_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    _require_bom_write(user)
    actor = _actor(user)
    is_admin = "admin" in user.get("roles", []) or "admin.access" in user.get("permissions", [])
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, draft_id, status, requested_by, response_json, verification_json, error_json,
                          result_bom_id, attempts, requested_at, started_at, finished_at, verified_at
                   FROM tplus_bom_submissions WHERE id=%s""",
                (submission_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="BOM 提交不存在")
            if not is_admin and str(row[3]) != actor:
                raise HTTPException(status_code=403, detail="permission denied")
            cur.execute(
                "SELECT event_type, detail_json, created_at FROM tplus_bom_submission_events WHERE submission_id=%s ORDER BY id",
                (submission_id,),
            )
            events = [{"type": item[0], "detail": item[1], "created_at": item[2]} for item in cur.fetchall()]
    return {
        "id": row[0], "draft_id": row[1], "status": row[2], "requested_by": row[3],
        "response": row[4], "verification": row[5], "error": row[6], "result_bom_id": row[7],
        "attempts": row[8], "requested_at": row[9], "started_at": row[10], "finished_at": row[11],
        "verified_at": row[12], "events": events,
    }
