"""质检报告域：元数据、单副本 WebDAV 文件、发布版本和可靠审计。"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import closing
from datetime import date
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation

from app.business_audit import write_business_audit
from app.core import _conn, require_login, require_permission
from app.quality_storage import StorageBackend, StorageError, WebDavStorage


router = APIRouter(prefix="/v1/quality-reports", tags=["quality-reports"])
_MAX_UPLOAD_BYTES = max(1, int(os.getenv("QUALITY_REPORT_MAX_UPLOAD_MB", "25"))) * 1024 * 1024
_SAFE_PART = re.compile(r"[^0-9A-Za-z._-]+")
_ALLOWED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


class QualityReportCreate(BaseModel):
    report_no: str = Field(min_length=1, max_length=120)
    product_code: str = Field(min_length=1, max_length=120)
    product_name: str = Field(min_length=1, max_length=240)
    batch_no: str | None = Field(default=None, max_length=120)
    report_type: str = Field(min_length=1, max_length=120)
    inspection_date: date | None = None
    issued_at: date | None = None
    revision: int = Field(default=1, ge=1, le=999)
    recipe_snapshot_id: str | None = Field(default=None, max_length=200)
    supersedes_report_id: int | None = None


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
    }


_REPORT_SELECT = """
SELECT id, report_no, product_code, product_name, batch_no, report_type,
       inspection_date, issued_at, revision, status, recipe_snapshot_id,
       supersedes_report_id, created_by, published_by, created_at, published_at
FROM quality_reports
"""


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
        clauses.append("(report_no ILIKE %s OR product_code ILIKE %s OR product_name ILIKE %s OR COALESCE(batch_no, '') ILIKE %s)")
        needle = f"%{q.strip()}%"
        params.extend([needle] * 4)
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
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO quality_reports(
                      report_no, product_code, product_name, batch_no, report_type,
                      inspection_date, issued_at, revision, recipe_snapshot_id,
                      supersedes_report_id, created_by
                    ) VALUES (%s, %s, %s, NULLIF(%s, ''), %s, %s, %s, %s, NULLIF(%s, ''), %s, %s)
                    RETURNING id
                    """,
                    (body.report_no.strip(), body.product_code.strip(), body.product_name.strip(),
                     (body.batch_no or "").strip(), body.report_type.strip(), body.inspection_date,
                     body.issued_at, body.revision, (body.recipe_snapshot_id or "").strip(),
                     body.supersedes_report_id, _uid(user)),
                )
                report_id = int(cur.fetchone()[0])
                write_business_audit(user=user, request=request, action="quality_report.create", resource_type="quality_report", resource_id=str(report_id), resource_revision=str(body.revision), required=True)
            conn.commit()
    except UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="同一报告编号和修订版本已存在") from exc
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
            cur.execute(
                "UPDATE quality_reports SET status = 'superseded', updated_at = NOW() WHERE report_no = %s AND id <> %s AND status = 'published'",
                (report["report_no"], report_id),
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
