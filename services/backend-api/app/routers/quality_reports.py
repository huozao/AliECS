"""质检报告域：元数据、单副本 WebDAV 文件、发布版本和可靠审计。"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import closing
from datetime import date
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation

from app.business_audit import write_business_audit
from app.core import _conn, require_login, require_permission
from app.quality_storage import StorageBackend, StorageError, WebDavStorage
from app.routers.exports import _latest_tplus_export_file


router = APIRouter(prefix="/v1/quality-reports", tags=["quality-reports"])
_MAX_UPLOAD_BYTES = max(1, int(os.getenv("QUALITY_REPORT_MAX_UPLOAD_MB", "25"))) * 1024 * 1024
_SAFE_PART = re.compile(r"[^0-9A-Za-z._-]+")
_ALLOWED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


class QualityReportCreate(BaseModel):
    external_report_no: str | None = Field(default=None, max_length=120)
    subject_source: Literal["tplus", "custom"]
    subject_type: Literal["raw_material", "finished_product", "custom_product"]
    product_code: str = Field(min_length=1, max_length=120)
    product_name: str = Field(min_length=1, max_length=240)
    batch_no: str | None = Field(default=None, max_length=120)
    report_source_code: str = Field(min_length=1, max_length=80)
    document_type_code: str = Field(min_length=1, max_length=80)
    test_item_codes: list[str] = Field(default_factory=list, max_length=50)
    material_category_code: str | None = Field(default=None, max_length=80)
    material_subcategory_code: str | None = Field(default=None, max_length=80)
    inspection_date: date | None = None
    issued_at: date | None = None
    revision: int = Field(default=1, ge=1, le=999)
    recipe_snapshot_id: str | None = Field(default=None, max_length=200)
    supersedes_report_id: int | None = None


class QualitySubjectCreate(BaseModel):
    subject_type: Literal["raw_material", "finished_product", "custom_product"] = "custom_product"
    code: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=240)
    specification: str | None = Field(default=None, max_length=240)
    material_category_code: str | None = Field(default=None, max_length=80)
    material_subcategory_code: str | None = Field(default=None, max_length=80)


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user["uid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="用户身份无效") from exc


def _can_manage(user: dict[str, Any]) -> bool:
    roles = user.get("roles", [])
    permissions = user.get("permissions", [])
    return "admin" in roles or "admin.access" in permissions or "quality_report.manage" in permissions


def _report_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0], "report_no": row[1], "product_code": row[2], "product_name": row[3],
        "batch_no": row[4], "report_type": row[5],
        "inspection_date": str(row[6]) if row[6] else None,
        "issued_at": str(row[7]) if row[7] else None,
        "revision": row[8], "status": row[9], "recipe_snapshot_id": row[10],
        "supersedes_report_id": row[11], "created_by": row[12], "published_by": row[13],
        "created_at": row[14].isoformat() if row[14] else None,
        "published_at": row[15].isoformat() if row[15] else None,
        "system_code": row[16] or row[1], "external_report_no": row[17],
        "subject_source": row[18], "subject_type": row[19],
        "report_source_code": row[20], "document_type_code": row[21],
        "material_category_code": row[22], "material_subcategory_code": row[23],
        "test_item_codes": list(row[24] or []),
    }


_REPORT_SELECT = """
SELECT id, report_no, product_code, product_name, batch_no, report_type,
       inspection_date, issued_at, revision, status, recipe_snapshot_id,
       supersedes_report_id, created_by, published_by, created_at, published_at
       , system_code, external_report_no, subject_source, subject_type,
       report_source_code, document_type_code, material_category_code,
       material_subcategory_code, test_item_codes
FROM quality_reports
"""


def _catalog_name(catalog_type: str, code: str) -> str:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM quality_report_catalog_items WHERE catalog_type = %s AND code = %s AND active",
                (catalog_type, code),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=422, detail=f"无效的质检分类：{catalog_type}/{code}")
    return str(row[0])


def _inventory_column(df, *names: str) -> str:
    for name in names:
        if name in df.columns:
            return name
    return ""


def _infer_subject_type(code: str, name: str, class_name: str) -> str:
    text = f"{class_name} {name}".lower()
    finished_words = ("色母", "母粒", "改性料", "改性塑料", "成品")
    package_words = ("色粉包", "助剂包")
    if any(word in text for word in finished_words) and not any(word in text for word in package_words):
        return "finished_product"
    raw_words = (
        "原料", "材料", "色粉", "颜料", "树脂", "助剂", "填充", "粉体", "钛白粉", "炭黑",
        "pp ", "pp-", "pe ", "abs树脂", "pc ", "pmma", "pa6", "pa66", "pbt", "pet ", "pom ",
    )
    if any(word in text for word in raw_words) or code.startswith(("0", "1", "5")):
        return "raw_material"
    return "finished_product"


def _tplus_subjects(q: str, limit: int) -> list[dict[str, Any]]:
    path = _latest_tplus_export_file("inventory")
    if path is None:
        raise HTTPException(status_code=404, detail="T+ 存货档案尚未同步")
    import pandas as pd

    df = pd.read_excel(path, dtype=str).fillna("")
    code_col = _inventory_column(df, "Code", "InventoryCode", "存货编码")
    name_col = _inventory_column(df, "Name", "InventoryName", "存货名称")
    class_code_col = _inventory_column(df, "InventoryClass.Code", "InventoryClassCode", "存货分类编码")
    class_name_col = _inventory_column(df, "InventoryClass.Name", "InventoryClassName", "存货分类")
    spec_col = _inventory_column(df, "Specification", "规格型号")
    disabled_col = _inventory_column(df, "Disabled", "停用")
    if not code_col or not name_col:
        raise HTTPException(status_code=409, detail="T+ 存货档案缺少编码或名称字段")
    if disabled_col:
        disabled = df[disabled_col].astype(str).str.strip().str.lower()
        df = df[~disabled.isin({"1", "true", "yes", "是"})]
    keyword = q.strip().lower()
    if keyword:
        mask = df[code_col].str.lower().str.contains(keyword, regex=False) | df[name_col].str.lower().str.contains(keyword, regex=False)
        if spec_col:
            mask |= df[spec_col].str.lower().str.contains(keyword, regex=False)
        df = df[mask]
    rows: list[dict[str, Any]] = []
    for _, row in df.drop_duplicates(subset=[code_col]).head(limit).iterrows():
        code = str(row[code_col]).strip()
        name = str(row[name_col]).strip()
        if not code or not name:
            continue
        class_name = str(row[class_name_col]).strip() if class_name_col else ""
        rows.append({
            "source": "tplus", "code": code, "name": name,
            "specification": str(row[spec_col]).strip() if spec_col else "",
            "inventory_class_code": str(row[class_code_col]).strip() if class_code_col else "",
            "inventory_class_name": class_name,
            "suggested_subject_type": _infer_subject_type(code, name, class_name),
        })
    return rows


def _fetch_report(report_id: int) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(_REPORT_SELECT + " WHERE id = %s", (report_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="质检报告不存在")
    return _report_dict(row)


def _ensure_visible(report: dict[str, Any], user: dict[str, Any]) -> None:
    if report["status"] != "published" and not _can_manage(user):
        raise HTTPException(status_code=404, detail="质检报告不存在")


@router.get("")
def list_quality_reports(
    request: Request,
    q: str = Query(default="", max_length=120),
    product_code: str = Query(default="", max_length=120),
    batch_no: str = Query(default="", max_length=120),
    report_type: str = Query(default="", max_length=120),
    status: str = Query(default="published", pattern="^(draft|published|superseded|revoked)$"),
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    if status != "published" and not _can_manage(user):
        raise HTTPException(status_code=403, detail="permission denied")
    clauses = ["status = %s"]
    params: list[Any] = [status]
    if q.strip():
        clauses.append("(report_no ILIKE %s OR COALESCE(system_code, '') ILIKE %s OR COALESCE(external_report_no, '') ILIKE %s OR product_code ILIKE %s OR product_name ILIKE %s OR COALESCE(batch_no, '') ILIKE %s)")
        needle = f"%{q.strip()}%"
        params.extend([needle] * 6)
    for column, value in (("product_code", product_code), ("batch_no", batch_no), ("report_type", report_type)):
        if value.strip():
            clauses.append(f"{column} ILIKE %s")
            params.append(f"%{value.strip()}%")
    if date_from:
        clauses.append("inspection_date >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("inspection_date <= %s")
        params.append(date_to)
    where = " AND ".join(clauses)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM quality_reports WHERE {where}", params)
            total = int(cur.fetchone()[0])
            cur.execute(
                _REPORT_SELECT + f" WHERE {where} ORDER BY inspection_date DESC NULLS LAST, id DESC LIMIT %s OFFSET %s",
                [*params, page_size, (page - 1) * page_size],
            )
            items = [_report_dict(row) for row in cur.fetchall()]
    safe_query = {"q": q.strip(), "product_code": product_code.strip(), "batch_no": batch_no.strip(), "report_type": report_type.strip(), "status": status}
    write_business_audit(user=user, request=request, action="quality_report.query", resource_type="quality_report", query=safe_query, result_count=total)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/catalog")
def quality_report_catalog(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("quality_report.read", user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT catalog_type, code, name, parent_code, description, sort_order
                FROM quality_report_catalog_items
                WHERE active
                ORDER BY catalog_type, sort_order, id
                """
            )
            rows = cur.fetchall()
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row[0]), []).append({
            "code": row[1], "name": row[2], "parent_code": row[3],
            "description": row[4], "sort_order": row[5],
        })
    return {"catalogs": groups}


@router.get("/subjects")
def quality_report_subjects(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    source: Literal["all", "tplus", "custom"] = Query(default="all"),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    require_permission("quality_report.manage", user)
    items: list[dict[str, Any]] = []
    if source in {"all", "tplus"}:
        items.extend(_tplus_subjects(q, limit))
    if source in {"all", "custom"}:
        needle = f"%{q.strip()}%"
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT code, name, specification, subject_type,
                           material_category_code, material_subcategory_code
                    FROM quality_subjects
                    WHERE active AND (%s = '%%' OR code ILIKE %s OR name ILIKE %s OR COALESCE(specification, '') ILIKE %s)
                    ORDER BY id DESC LIMIT %s
                    """,
                    (needle, needle, needle, needle, limit),
                )
                for row in cur.fetchall():
                    items.append({
                        "source": "custom", "code": row[0], "name": row[1],
                        "specification": row[2] or "", "suggested_subject_type": row[3],
                        "material_category_code": row[4], "material_subcategory_code": row[5],
                        "inventory_class_code": "", "inventory_class_name": "自定义档案",
                    })
    return {"items": items[:limit], "total": min(len(items), limit)}


@router.post("/subjects")
def create_quality_subject(
    request: Request,
    body: QualitySubjectCreate,
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    require_permission("quality_report.manage", user)
    if body.material_category_code:
        _catalog_name("material_category", body.material_category_code)
    if body.material_subcategory_code:
        _catalog_name("material_subcategory", body.material_subcategory_code)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            code = (body.code or "").strip().upper()
            if not code:
                cur.execute("SELECT nextval('quality_custom_subject_code_seq')")
                code = f"CUSTOM-{int(cur.fetchone()[0]):06d}"
            try:
                cur.execute(
                    """
                    INSERT INTO quality_subjects(
                      subject_type, code, name, specification, material_category_code,
                      material_subcategory_code, created_by
                    ) VALUES (%s, %s, %s, NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''), %s)
                    RETURNING id
                    """,
                    (body.subject_type, code, body.name.strip(), (body.specification or "").strip(),
                     (body.material_category_code or "").strip(), (body.material_subcategory_code or "").strip(), _uid(user)),
                )
                subject_id = int(cur.fetchone()[0])
                write_business_audit(user=user, request=request, action="quality_report.subject.create", resource_type="quality_subject", resource_id=str(subject_id), detail={"code": code}, required=True)
                conn.commit()
            except UniqueViolation as exc:
                conn.rollback()
                raise HTTPException(status_code=409, detail="自定义物料编码已存在") from exc
    return {"id": subject_id, "source": "custom", "code": code, "name": body.name.strip(), "specification": (body.specification or "").strip(), "suggested_subject_type": body.subject_type, "material_category_code": body.material_category_code, "material_subcategory_code": body.material_subcategory_code}


@router.get("/manage/drafts")
def quality_report_drafts(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("quality_report.manage", user)
    admin = "admin" in user.get("roles", []) or "admin.access" in user.get("permissions", [])
    owner_clause = "" if admin else " AND created_by = %s"
    params: list[Any] = [] if admin else [_uid(user)]
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                _REPORT_SELECT + f"""
                WHERE status = 'draft'{owner_clause}
                ORDER BY created_at DESC LIMIT 30
                """,
                params,
            )
            reports = [_report_dict(row) for row in cur.fetchall()]
            ids = [item["id"] for item in reports]
            counts: dict[int, int] = {}
            if ids:
                cur.execute(
                    "SELECT report_id, COUNT(*) FROM quality_report_files WHERE report_id = ANY(%s) AND status = 'active' GROUP BY report_id",
                    (ids,),
                )
                counts = {int(row[0]): int(row[1]) for row in cur.fetchall()}
    for report in reports:
        report["file_count"] = counts.get(report["id"], 0)
    return {"items": reports}


@router.get("/{report_id}")
def quality_report_detail(request: Request, report_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    report = _fetch_report(report_id)
    _ensure_visible(report, user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            history_clause = "" if _can_manage(user) else " AND status <> 'draft'"
            cur.execute(
                _REPORT_SELECT + f" WHERE report_no = %s{history_clause} ORDER BY revision DESC, id DESC",
                (report["report_no"],),
            )
            history = [_report_dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT id, filename, mime_type, size_bytes, sha256, status, created_at
                FROM quality_report_files WHERE report_id = %s AND status = 'active' ORDER BY id
                """,
                (report_id,),
            )
            files = [
                {"id": row[0], "filename": row[1], "mime_type": row[2], "size_bytes": row[3], "sha256": row[4], "status": row[5], "created_at": row[6].isoformat()}
                for row in cur.fetchall() if _can_manage(user) or row[2] == "application/pdf"
            ]
    write_business_audit(user=user, request=request, action="quality_report.view", resource_type="quality_report", resource_id=str(report_id), resource_revision=str(report["revision"]))
    return {**report, "files": files, "history": history}


@router.post("")
def create_quality_report(request: Request, body: QualityReportCreate, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("quality_report.manage", user)
    report_source_name = _catalog_name("report_source", body.report_source_code)
    document_type_name = _catalog_name("document_type", body.document_type_code)
    if body.material_category_code:
        _catalog_name("material_category", body.material_category_code)
    if body.material_subcategory_code:
        _catalog_name("material_subcategory", body.material_subcategory_code)
    clean_test_items = list(dict.fromkeys(code.strip() for code in body.test_item_codes if code.strip()))
    for code in clean_test_items:
        _catalog_name("test_item", code)
    if body.subject_source == "tplus":
        matches = [item for item in _tplus_subjects(body.product_code.strip(), 10) if item["code"] == body.product_code.strip()]
        if not matches or matches[0]["name"] != body.product_name.strip():
            raise HTTPException(status_code=422, detail="请选择当前 T+ 存货档案中的物料，编码和名称不能手工组合")
    else:
        with closing(_conn()) as subject_conn:
            with subject_conn.cursor() as cur:
                cur.execute("SELECT name FROM quality_subjects WHERE code = %s AND active", (body.product_code.strip(),))
                row = cur.fetchone()
        if not row or str(row[0]) != body.product_name.strip():
            raise HTTPException(status_code=422, detail="请选择已登记的自定义产品")
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO quality_report_daily_sequences(report_date, last_value)
                    VALUES ((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date, 1)
                    ON CONFLICT(report_date) DO UPDATE
                    SET last_value = quality_report_daily_sequences.last_value + 1
                    WHERE quality_report_daily_sequences.last_value < 999
                    RETURNING to_char(report_date, 'YYYYMMDD') || lpad(last_value::text, 3, '0')
                    """
                )
                sequence_row = cur.fetchone()
                if not sequence_row:
                    raise HTTPException(status_code=409, detail="当日质检报告数量已达到 999 份")
                system_code = str(sequence_row[0])
                cur.execute(
                    """
                    INSERT INTO quality_reports(
                      report_no, system_code, external_report_no,
                      subject_source, subject_type, product_code, product_name, batch_no,
                      report_type, report_source_code, document_type_code, test_item_codes,
                      material_category_code, material_subcategory_code,
                      inspection_date, issued_at, revision, recipe_snapshot_id,
                      supersedes_report_id, created_by
                    ) VALUES (
                      %s, %s, NULLIF(%s, ''), %s, %s, %s, %s, NULLIF(%s, ''),
                      %s, %s, %s, %s, NULLIF(%s, ''), NULLIF(%s, ''),
                      %s, %s, %s, NULLIF(%s, ''), %s, %s
                    )
                    RETURNING id
                    """,
                    (system_code, system_code, (body.external_report_no or "").strip(),
                     body.subject_source, body.subject_type, body.product_code.strip(), body.product_name.strip(),
                     (body.batch_no or "").strip(), document_type_name, body.report_source_code,
                     body.document_type_code, clean_test_items, (body.material_category_code or "").strip(),
                     (body.material_subcategory_code or "").strip(), body.inspection_date, body.issued_at,
                     body.revision, (body.recipe_snapshot_id or "").strip(), body.supersedes_report_id, _uid(user)),
                )
                report_id = int(cur.fetchone()[0])
                write_business_audit(user=user, request=request, action="quality_report.create", resource_type="quality_report", resource_id=str(report_id), resource_revision=str(body.revision), detail={"system_code": system_code, "report_source": report_source_name, "document_type": document_type_name}, required=True)
            conn.commit()
    except UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="质检报告系统编号冲突，请重试") from exc
    return _fetch_report(report_id)


def _reserve_backend(size_bytes: int) -> StorageBackend:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quality_storage_backends
                SET uploaded_bytes_month = 0,
                    upload_month = date_trunc('month', CURRENT_DATE)::date,
                    updated_at = NOW()
                WHERE upload_month <> date_trunc('month', CURRENT_DATE)::date
                """
            )
            cur.execute(
                """
                SELECT id, code, provider, display_name, credential_ref, base_path
                FROM quality_storage_backends
                WHERE status = 'active'
                  AND uploaded_bytes_month + %s <= monthly_upload_limit_bytes
                ORDER BY (uploaded_bytes_month::numeric / monthly_upload_limit_bytes), priority, id
                FOR UPDATE SKIP LOCKED LIMIT 1
                """,
                (size_bytes,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=507, detail="本月质检报告上传额度不足")
            cur.execute(
                "UPDATE quality_storage_backends SET uploaded_bytes_month = uploaded_bytes_month + %s, updated_at = NOW() WHERE id = %s",
                (size_bytes, row[0]),
            )
        conn.commit()
    return StorageBackend(*row)


def _release_reservation(backend_id: int, size_bytes: int) -> None:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE quality_storage_backends SET uploaded_bytes_month = GREATEST(0, uploaded_bytes_month - %s), updated_at = NOW() WHERE id = %s",
                    (size_bytes, backend_id),
                )
            conn.commit()
    except Exception:
        pass


@router.post("/{report_id}/files")
def upload_quality_report_file(
    request: Request,
    report_id: int,
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    require_permission("quality_report.manage", user)
    report = _fetch_report(report_id)
    if report["status"] != "draft":
        raise HTTPException(status_code=409, detail="只有草稿报告可以上传文件")
    data = file.file.read(_MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"文件不能超过 {_MAX_UPLOAD_BYTES // 1024 // 1024}MB")
    mime_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    extension = _ALLOWED_MIME_TYPES.get(mime_type)
    if not extension:
        raise HTTPException(status_code=415, detail="仅支持 PDF、XLSX 和 DOCX 文件")
    digest = hashlib.sha256(data).hexdigest()
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM quality_report_files WHERE report_id = %s AND sha256 = %s", (report_id, digest))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="该文件已上传到当前报告")
    backend = _reserve_backend(len(data))
    safe_code = _SAFE_PART.sub("_", report["product_code"]).strip("._") or "product"
    year = (report["inspection_date"] or str(date.today()))[:4]
    remote_path = f"{backend.base_path}/{safe_code}/{year}/{report_id}/v{report['revision']}/{digest[:16]}{extension}"
    storage: WebDavStorage | None = None
    try:
        storage = WebDavStorage(backend)
        storage.put(remote_path, data, mime_type)
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO quality_report_files(
                      report_id, storage_backend_id, remote_path, filename, mime_type,
                      size_bytes, sha256, uploaded_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (report_id, backend.id, remote_path, (file.filename or f"report{extension}")[:240], mime_type, len(data), digest, _uid(user)),
                )
                file_id = int(cur.fetchone()[0])
                write_business_audit(user=user, request=request, action="quality_report.upload", resource_type="quality_report_file", resource_id=str(file_id), resource_revision=str(report["revision"]), file_sha256=digest, detail={"report_id": report_id, "size_bytes": len(data), "storage_backend": backend.code}, required=True)
            conn.commit()
    except Exception:
        if storage is not None:
            storage.delete(remote_path)
        _release_reservation(backend.id, len(data))
        raise
    return {"id": file_id, "filename": file.filename, "mime_type": mime_type, "size_bytes": len(data), "sha256": digest, "storage_backend": backend.code}


@router.post("/{report_id}/publish")
def publish_quality_report(request: Request, report_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("quality_report.admin", user)
    report = _fetch_report(report_id)
    if report["status"] != "draft":
        raise HTTPException(status_code=409, detail="只有草稿报告可以发布")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM quality_report_files WHERE report_id = %s AND status = 'active'", (report_id,))
            if int(cur.fetchone()[0]) == 0:
                raise HTTPException(status_code=409, detail="报告至少需要一个有效文件")
            if report["supersedes_report_id"]:
                cur.execute(
                    "UPDATE quality_reports SET status = 'superseded', updated_at = NOW() WHERE id = %s AND status = 'published'",
                    (report["supersedes_report_id"],),
                )
            cur.execute(
                "UPDATE quality_reports SET status = 'published', published_by = %s, published_at = NOW(), updated_at = NOW() WHERE id = %s",
                (_uid(user), report_id),
            )
            write_business_audit(user=user, request=request, action="quality_report.publish", resource_type="quality_report", resource_id=str(report_id), resource_revision=str(report["revision"]), required=True)
        conn.commit()
    return _fetch_report(report_id)


@router.post("/{report_id}/revoke")
def revoke_quality_report(request: Request, report_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("quality_report.admin", user)
    report = _fetch_report(report_id)
    if report["status"] not in {"published", "superseded"}:
        raise HTTPException(status_code=409, detail="只有已发布或已替换报告可以作废")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE quality_reports SET status = 'revoked', updated_at = NOW() WHERE id = %s", (report_id,))
            write_business_audit(user=user, request=request, action="quality_report.revoke", resource_type="quality_report", resource_id=str(report_id), resource_revision=str(report["revision"]), required=True)
        conn.commit()
    return _fetch_report(report_id)


@router.get("/files/{file_id}/download")
def download_quality_report_file(request: Request, file_id: int, user: dict[str, Any] = Depends(require_login)) -> StreamingResponse:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.remote_path, f.filename, f.mime_type, f.size_bytes, f.sha256,
                       r.id, r.revision, r.status,
                       b.id, b.code, b.provider, b.display_name, b.credential_ref, b.base_path
                FROM quality_report_files f
                JOIN quality_reports r ON r.id = f.report_id
                JOIN quality_storage_backends b ON b.id = f.storage_backend_id
                WHERE f.id = %s AND f.status = 'active'
                """,
                (file_id,),
            )
            row = cur.fetchone()
    if not row or (row[7] != "published" and not _can_manage(user)):
        raise HTTPException(status_code=404, detail="质检报告文件不存在")
    if row[2] != "application/pdf" and not _can_manage(user):
        raise HTTPException(status_code=404, detail="质检报告文件不存在")
    backend = StorageBackend(*row[8:14])
    try:
        storage_response, chunks = WebDavStorage(backend).stream(row[0])
    except StorageError as exc:
        write_business_audit(user=user, request=request, action="quality_report.download", resource_type="quality_report_file", resource_id=str(file_id), resource_revision=str(row[6]), file_sha256=row[4], outcome="failed", error_code="storage_read_failed", required=False)
        raise HTTPException(status_code=502, detail="质检报告文件读取失败") from exc
    try:
        write_business_audit(user=user, request=request, action="quality_report.download", resource_type="quality_report_file", resource_id=str(file_id), resource_revision=str(row[6]), file_sha256=row[4], detail={"report_id": row[5], "size_bytes": row[3]}, required=True)
    except Exception:
        storage_response.close()
        raise
    disposition = f"attachment; filename=quality-report; filename*=UTF-8''{quote(row[1])}"
    return StreamingResponse(chunks, media_type=row[2], headers={"Content-Length": str(row[3]), "Content-Disposition": disposition})


@router.post("/storage/health-check")
def quality_storage_health_check(request: Request, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("quality_report.admin", user)
    results: list[dict[str, Any]] = []
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, code, provider, display_name, credential_ref, base_path FROM quality_storage_backends WHERE status <> 'disabled' ORDER BY priority, id")
            backends = [StorageBackend(*row) for row in cur.fetchall()]
    for backend in backends:
        error = None
        try:
            WebDavStorage(backend).health_check()
        except StorageError as exc:
            error = str(exc)[:500]
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE quality_storage_backends SET status = %s, last_health_check_at = NOW(), last_health_error = %s, updated_at = NOW() WHERE id = %s",
                    ("degraded" if error else "active", error, backend.id),
                )
            conn.commit()
        results.append({"code": backend.code, "ok": not error, "error": error})
    write_business_audit(user=user, request=request, action="quality_storage.health_check", resource_type="quality_storage", result_count=len(results), detail={"failed": sum(1 for item in results if not item["ok"])}, required=True)
    return {"items": results}
