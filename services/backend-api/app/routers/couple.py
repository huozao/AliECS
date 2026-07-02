"""Couple私密空间域：回忆/相册/照片存储、Immich资产、纪念日、心愿单、分享链接与couple空间管理。"""

from __future__ import annotations

import calendar
import json
import os
import secrets
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Header, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.core import _audit, _conn, _couple_route, _has_couple_access, _request_logger, get_current_user, require_admin, require_login, require_permission
from app.logging_utils import log_event


router = APIRouter()

class MemoryUpsertRequest(BaseModel):
    couple_space_id: int | None = None
    title: str
    content: str | None = None
    memory_date: str | None = None
    place_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    cover_photo_url: str | None = None
    visibility: str = "private"
    tags: list[str] = Field(default_factory=list)


class ImmichAssetBindRequest(BaseModel):
    asset_ids: list[str] = Field(default_factory=list)
    immich_asset_id: str | None = None
    immich_album_id: str | None = None
    original_filename: str | None = None
    taken_at: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    thumbnail_cache_key: str | None = None
    sort_order: int = 0


class PatchCoupleSpaceRequest(BaseModel):
    name: str | None = None
    start_date: str | None = None
    theme: str | None = None
    cover_image_url: str | None = None


class AnniversaryUpsertRequest(BaseModel):
    couple_space_id: int | None = None
    title: str
    date: str
    repeat_type: str = "yearly"
    description: str | None = None


class BucketItemUpsertRequest(BaseModel):
    couple_space_id: int | None = None
    title: str
    description: str | None = None
    status: str = "want"
    target_date: str | None = None
    completed_memory_id: int | None = None


class CreateShareLinkRequest(BaseModel):
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


ALLOWED_UPLOAD_MIMES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "image/gif": {".gif"},
}


def _max_upload_bytes() -> int:
    raw = os.getenv("MAX_UPLOAD_MB", os.getenv("PHOTO_MAX_UPLOAD_MB", "15"))
    try:
        mb = max(1, int(raw))
    except ValueError:
        mb = 15
    return mb * 1024 * 1024


def _detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validate_photo_upload(filename: str | None, declared_mime: str | None, content: bytes) -> tuple[str, str]:
    ext = Path(filename or "").suffix.lower()
    detected_mime = _detect_image_mime(content)
    declared = (declared_mime or "").split(";", 1)[0].strip().lower()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")
    if len(content) > _max_upload_bytes():
        raise HTTPException(status_code=400, detail="file too large")
    if not detected_mime or detected_mime not in ALLOWED_UPLOAD_MIMES:
        raise HTTPException(status_code=400, detail="unsupported file type")
    if ext not in ALLOWED_UPLOAD_MIMES[detected_mime]:
        raise HTTPException(status_code=400, detail="file extension does not match image type")
    if declared and declared not in ALLOWED_UPLOAD_MIMES:
        raise HTTPException(status_code=400, detail="unsupported file type")
    if declared and declared != detected_mime:
        raise HTTPException(status_code=400, detail="mime type does not match file content")
    return ext, detected_mime


def _public_upload_url(filename: str) -> str:
    public_base = os.getenv("APP_BASE_URL", "").rstrip("/")
    relative_url = f"/uploads/{filename}"
    return f"{public_base}{relative_url}" if public_base else relative_url


def _public_photo_content_url(key: str) -> str:
    public_base = os.getenv("APP_BASE_URL", "").rstrip("/")
    relative_url = f"/api/v1/photos/content/{urllib.parse.quote(key, safe='')}"
    return f"{public_base}{relative_url}" if public_base else relative_url


def _webdock_photo_base_url() -> str:
    return os.getenv("WEBDOCK_PHOTO_BASE_URL", "http://host.docker.internal:11800").rstrip("/")


def _webdock_photo_token() -> str:
    token = os.getenv("WEBDOCK_PHOTO_API_TOKEN") or os.getenv("WEB_DOCK_API_TOKEN") or ""
    if not token:
        raise HTTPException(status_code=500, detail="WEBDOCK_PHOTO_API_TOKEN is required")
    return token


def _webdock_photo_timeout() -> int:
    raw = os.getenv("WEBDOCK_PHOTO_TIMEOUT_SECONDS", "30")
    try:
        return max(1, int(raw))
    except ValueError:
        return 30


def _webdock_photo_url(key: str | None = None) -> str:
    if key is None:
        return f"{_webdock_photo_base_url()}/storage/photos"
    return f"{_webdock_photo_base_url()}/storage/photos/{urllib.parse.quote(key, safe='')}"


def _multipart_photo_body(filename: str | None, content_type: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----aliecs-{uuid.uuid4().hex}"
    safe_name = (filename or "photo").replace("\\", "_").replace('"', "_")
    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8"),
        content,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _webdock_photo_request(
    method: str,
    key: str | None = None,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    request_headers = {"Authorization": f"Bearer {_webdock_photo_token()}"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        _webdock_photo_url(key),
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=_webdock_photo_timeout()) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise HTTPException(status_code=404, detail="photo not found") from exc
        raise HTTPException(status_code=502, detail=f"webdock photo storage returned {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail="webdock photo storage unavailable") from exc


def _webdock_photo_key(original_storage_url: str | None) -> str | None:
    if not original_storage_url or not original_storage_url.startswith("webdock:"):
        return None
    key = original_storage_url.split(":", 1)[1].strip()
    return key or None


class PhotoStorage:
    driver = "local"

    async def save(self, file: UploadFile) -> dict[str, str]:
        raise NotImplementedError

    def delete(self, original_storage_url: str | None) -> None:
        return None


def _upload_disk_usage(path: Path | str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        # The upload dir is created lazily by LocalPhotoStorage, so it may not
        # exist yet at healthcheck time. A missing/unstat-able path must not make
        # /healthz fail — report it as unavailable instead of raising.
        return {"path": str(path), "available": False, "percent": None, "error": str(exc)}
    percent = round(usage.used * 100 / usage.total, 1) if usage.total else 0.0
    return {
        "path": str(path),
        "available": True,
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": percent,
    }


class LocalPhotoStorage(PhotoStorage):
    driver = "local"

    def __init__(self) -> None:
        self.base_dir = Path(os.getenv("LOCAL_UPLOAD_DIR", "/app/uploads"))
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def save(self, file: UploadFile) -> dict[str, str]:
        content = await file.read()
        return self.save_content(file.filename, file.content_type, content)

    def save_content(self, filename: str | None, content_type: str | None, content: bytes) -> dict[str, str]:
        ext, _mime = _validate_photo_upload(filename, content_type, content)
        filename = f"{uuid.uuid4().hex}{ext}"
        full_path = self.base_dir / filename
        full_path.write_bytes(content)
        self._warn_if_disk_high()
        public_url = _public_upload_url(filename)
        return {
            "original_storage_url": str(full_path),
            "display_url": public_url,
            "thumbnail_url": public_url,
            "storage_driver": self.driver,
        }

    def delete(self, original_storage_url: str | None) -> None:
        if original_storage_url:
            Path(original_storage_url).unlink(missing_ok=True)

    def _warn_if_disk_high(self) -> None:
        info = _upload_disk_usage(self.base_dir)
        if info.get("percent") is None:
            return
        raw = os.getenv("UPLOAD_DISK_WARN_PCT", "").strip()
        if not raw:
            return
        try:
            threshold = float(raw)
        except ValueError:
            return
        if info["percent"] >= threshold:
            log_event(
                _request_logger,
                "upload disk usage high",
                path=info["path"],
                percent=info["percent"],
                threshold=threshold,
            )


class OssPhotoStorage(PhotoStorage):
    driver = "oss"

    def __init__(self) -> None:
        from app.oss_client import OssClient, config_from_env

        config = config_from_env()
        if not config.enabled:
            self._client = None
        else:
            self._client = OssClient(config)

    async def save(self, file: UploadFile) -> dict[str, str]:
        content = await file.read()
        if self._client is None:
            raise HTTPException(status_code=501, detail="OSS storage is not configured in this build")

        ext, mime = _validate_photo_upload(file.filename, file.content_type, content)
        key = f"couple/{uuid.uuid4().hex}{ext}"
        from app.oss_client import OssError

        try:
            self._client.put_object(key, content, mime)
        except OssError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        public_url = self._client.object_url(key)
        return {
            "original_storage_url": f"oss:{key}",
            "display_url": public_url,
            "thumbnail_url": public_url,
            "storage_driver": self.driver,
        }

    def delete(self, original_storage_url: str | None) -> None:
        if not original_storage_url or not original_storage_url.startswith("oss:"):
            return
        if self._client is None:
            return
        key = original_storage_url.split(":", 1)[1]
        from app.oss_client import OssError

        try:
            self._client.delete_object(key)
        except OssError:
            pass


class WebDockPhotoStorage(PhotoStorage):
    driver = "webdock"

    async def save(self, file: UploadFile) -> dict[str, str]:
        content = await file.read()
        _ext, mime = _validate_photo_upload(file.filename, file.content_type, content)
        body, content_type = _multipart_photo_body(file.filename, mime, content)
        try:
            raw = _webdock_photo_request("POST", body=body, headers={"Content-Type": content_type})
        except HTTPException as exc:
            if exc.status_code == 502:
                return LocalPhotoStorage().save_content(file.filename, mime, content)
            raise
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=502, detail="invalid webdock photo storage response") from exc
        key = str(payload.get("key") or "").strip()
        if not key:
            raise HTTPException(status_code=502, detail="webdock photo storage did not return key")
        public_url = _public_photo_content_url(key)
        return {
            "original_storage_url": f"webdock:{key}",
            "display_url": public_url,
            "thumbnail_url": public_url,
            "storage_driver": self.driver,
        }

    def delete(self, original_storage_url: str | None) -> None:
        key = _webdock_photo_key(original_storage_url)
        if key:
            _webdock_photo_request("DELETE", key)


def photo_storage() -> PhotoStorage:
    driver = os.getenv("STORAGE_DRIVER", "local").strip().lower() or "local"
    if driver == "local":
        return LocalPhotoStorage()
    if driver == "oss":
        return OssPhotoStorage()
    if driver == "webdock":
        return WebDockPhotoStorage()
    raise HTTPException(status_code=500, detail="invalid STORAGE_DRIVER")


@router.get("/uploads/{name}")
def serve_upload(name: str) -> FileResponse:
    base = Path(os.getenv("LOCAL_UPLOAD_DIR", "/app/uploads")).resolve()
    target = (base / name).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


class CreateCoupleSpaceRequest(BaseModel):
    name: str
    start_date: str | None = None
    theme: str | None = None


class AddCoupleMemberRequest(BaseModel):
    username: str
    role: str = "member"


@router.get("/couple/access")
def couple_access(authorization: str | None = Header(default=None)) -> dict[str, Any] | None:
    if not authorization:
        raise HTTPException(status_code=404, detail="not found")

    try:
        user = get_current_user(authorization)
    except HTTPException:
        raise HTTPException(status_code=404, detail="not found")

    if not _has_couple_access(user):
        raise HTTPException(status_code=404, detail="not found")

    return {"allowed": True, "route": _couple_route()}


@router.get("/v1/immich/status")
def immich_status(user: dict[str, Any] = Depends(require_login)) -> dict[str, object]:
    require_permission("couple_memory_access", user)
    from app.immich_client import ImmichClient

    return ImmichClient().status()


def _public_immich_thumbnail_url(asset_id: str) -> str:
    public_base = os.getenv("APP_BASE_URL", "").rstrip("/")
    relative_url = f"/api/v1/immich/assets/{urllib.parse.quote(asset_id, safe='')}/thumbnail"
    return f"{public_base}{relative_url}" if public_base else relative_url


@router.get("/v1/immich/assets")
def immich_assets(
    query: str | None = Query(default=None),
    taken_after: str | None = Query(default=None),
    taken_before: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    require_permission("couple_memory_access", user)
    from app.immich_client import ImmichClient, load_immich_config

    config = load_immich_config()
    if not config.enabled:
        return {"enabled": False, "items": []}
    try:
        assets = ImmichClient(config).search_assets(
            query=query,
            taken_after=taken_after,
            taken_before=taken_before,
            page=page,
        )
    except Exception as exc:
        return {"enabled": True, "items": [], "detail": str(exc)}
    return {
        "enabled": True,
        "items": [
            {
                "asset_id": asset.asset_id,
                "original_filename": asset.original_filename,
                "taken_at": asset.taken_at,
                "latitude": asset.latitude,
                "longitude": asset.longitude,
                "thumbnail_url": _public_immich_thumbnail_url(asset.asset_id),
            }
            for asset in assets
            if asset.asset_id
        ],
    }


@router.get("/v1/immich/assets/{asset_id}/thumbnail")
def immich_asset_thumbnail(asset_id: str, user: dict[str, Any] = Depends(require_login)) -> Response:
    require_permission("couple_memory_access", user)
    from app.immich_client import ImmichClient

    try:
        content, content_type = ImmichClient().get_thumbnail(asset_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=content, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})


def _user_id_by_username(username: str) -> int | None:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
    return row[0] if row else None


def _resolve_couple_space_id(user_id: int, requested_space_id: int | None) -> int:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            if requested_space_id:
                cur.execute(
                    """
                    SELECT couple_space_id
                    FROM couple_members
                    WHERE user_id = %s AND couple_space_id = %s
                    """,
                    (user_id, requested_space_id),
                )
                row = cur.fetchone()
                if row:
                    return row[0]
                raise HTTPException(status_code=403, detail="permission denied")

            cur.execute(
                """
                SELECT couple_space_id
                FROM couple_members
                WHERE user_id = %s
                ORDER BY joined_at ASC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if row:
                return row[0]
    raise HTTPException(status_code=404, detail="not found")


def _require_couple_user(user: dict[str, Any]) -> int:
    if not _has_couple_access(user):
        raise HTTPException(status_code=403, detail="permission denied")
    user_id = _user_id_by_username(str(user.get("sub", "")))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user")
    return user_id


def _is_space_owner(user: dict[str, Any], user_id: int, space_id: int) -> bool:
    roles = [str(r).lower() for r in user.get("roles", [])]
    permissions = [str(p).lower() for p in user.get("permissions", [])]
    if "admin" in roles or "admin.access" in permissions:
        return True
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role
                FROM couple_members
                WHERE couple_space_id = %s AND user_id = %s
                LIMIT 1
                """,
                (space_id, user_id),
            )
            row = cur.fetchone()
    return bool(row and row[0] == "owner")


def _parse_date_or_none(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid date") from exc


def _normalize_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in tags:
        tag = str(raw).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        cleaned.append(tag[:40])
    return cleaned


def _safe_date(year: int, month: int, day: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def _next_anniversary_occurrence(base_date: date, repeat_type: str, today: date | None = None) -> date | None:
    today = today or date.today()
    if repeat_type == "none":
        return base_date if base_date >= today else None
    if repeat_type == "yearly":
        current = _safe_date(today.year, base_date.month, base_date.day)
        if current < today:
            current = _safe_date(today.year + 1, base_date.month, base_date.day)
        return current
    if repeat_type == "monthly":
        current = _safe_date(today.year, today.month, base_date.day)
        if current < today:
            year = today.year + (1 if today.month == 12 else 0)
            month = 1 if today.month == 12 else today.month + 1
            current = _safe_date(year, month, base_date.day)
        return current
    raise HTTPException(status_code=400, detail="invalid repeat_type")


def _anniversary_payload(row: tuple[Any, ...], today: date | None = None) -> dict[str, Any] | None:
    occurrence = _next_anniversary_occurrence(row[2], row[3], today)
    if occurrence is None:
        return None
    today = today or date.today()
    return {
        "id": row[0],
        "title": row[1],
        "date": str(row[2]),
        "repeat_type": row[3],
        "description": row[4],
        "next_occurrence": str(occurrence),
        "days_remaining": (occurrence - today).days,
    }


def _share_base_url() -> str:
    return os.getenv("SHARE_BASE_URL", os.getenv("APP_BASE_URL", "")).rstrip("/")


_share_hits: dict[str, list[float]] = {}


def _check_share_rate(token: str) -> None:
    now = time.monotonic()
    hits = [ts for ts in _share_hits.get(token, []) if now - ts < 60]
    hits.append(now)
    _share_hits[token] = hits
    if len(hits) > 120:
        raise HTTPException(status_code=429, detail="too many requests")


@router.get("/v1/memories")
def list_memories(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    couple_space_id: int | None = Query(default=None),
    archived: str = Query(default="false"),
    tag: str | None = Query(default=None),
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    q: str | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    offset = (page - 1) * page_size
    where = ["m.couple_space_id = %s"]
    params: list[Any] = [space_id]
    archived_value = archived.strip().lower()
    if archived_value not in {"all", "true", "1", "yes"}:
        where.append("m.archived = false")
    elif archived_value in {"true", "1", "yes"}:
        where.append("m.archived = true")
    if tag:
        where.append("EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id = m.id AND mt.tag = %s)")
        params.append(tag.strip())
    start = _parse_date_or_none(from_date)
    end = _parse_date_or_none(to_date)
    if start:
        where.append("m.memory_date >= %s")
        params.append(start)
    if end:
        where.append("m.memory_date <= %s")
        params.append(end)
    if q and q.strip():
        where.append("(m.title ILIKE %s OR COALESCE(m.content, '') ILIKE %s)")
        pattern = f"%{q.strip()}%"
        params.extend([pattern, pattern])
    where_sql = " AND ".join(where)

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM memories m WHERE {where_sql}", params)
            total = cur.fetchone()[0]
            cur.execute(
                f"""
                SELECT id, couple_space_id, title, content, memory_date, place_name, latitude, longitude,
                       cover_photo_url, visibility, created_by, created_at, updated_at, archived,
                       COALESCE((SELECT COUNT(*) FROM photos p WHERE p.memory_id = m.id), 0) AS photo_count
                FROM memories m
                WHERE {where_sql}
                ORDER BY memory_date DESC NULLS LAST, id DESC
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, offset],
            )
            rows = cur.fetchall()

            memory_ids = [row[0] for row in rows]
            tags_map: dict[int, list[str]] = {mid: [] for mid in memory_ids}
            if memory_ids:
                cur.execute(
                    "SELECT memory_id, tag FROM memory_tags WHERE memory_id = ANY(%s::bigint[]) ORDER BY id",
                    (memory_ids,),
                )
                for mid, tag in cur.fetchall():
                    tags_map.setdefault(mid, []).append(tag)

    return {
        "items": [
            {
                "id": row[0],
                "couple_space_id": row[1],
                "title": row[2],
                "content": row[3],
                "memory_date": str(row[4]) if row[4] else None,
                "place_name": row[5],
                "latitude": row[6],
                "longitude": row[7],
                "cover_photo_url": row[8],
                "visibility": row[9],
                "created_by": row[10],
                "created_at": str(row[11]),
                "updated_at": str(row[12]),
                "archived": row[13],
                "photo_count": row[14],
                "tags": tags_map.get(row[0], []),
            }
            for row in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.post("/v1/memories")
def create_memory(body: MemoryUpsertRequest, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, body.couple_space_id)
    if body.visibility not in {"private", "shareable"}:
        raise HTTPException(status_code=400, detail="invalid visibility")
    memory_date = _parse_date_or_none(body.memory_date)

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories(
                    couple_space_id, title, content, memory_date, place_name, latitude, longitude,
                    cover_photo_url, visibility, created_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    space_id,
                    body.title,
                    body.content,
                    memory_date,
                    body.place_name,
                    body.latitude,
                    body.longitude,
                    body.cover_photo_url,
                    body.visibility,
                    user_id,
                ),
            )
            memory_id = cur.fetchone()[0]
            for tag_clean in _normalize_tags(body.tags):
                cur.execute(
                    "INSERT INTO memory_tags(memory_id, tag) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (memory_id, tag_clean),
                )
        conn.commit()
    return {"id": memory_id}


@router.get("/v1/memories/{memory_id}")
def get_memory(memory_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id, m.couple_space_id, m.title, m.content, m.memory_date, m.place_name, m.latitude, m.longitude,
                       m.cover_photo_url, m.visibility, m.created_by, m.created_at, m.updated_at, m.archived,
                       COALESCE((SELECT COUNT(*) FROM photos p WHERE p.memory_id = m.id), 0) AS photo_count
                FROM memories m
                JOIN couple_members cm ON cm.couple_space_id = m.couple_space_id
                WHERE m.id = %s AND cm.user_id = %s
                LIMIT 1
                """,
                (memory_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
            cur.execute("SELECT tag FROM memory_tags WHERE memory_id = %s ORDER BY id", (memory_id,))
            tags = [r[0] for r in cur.fetchall()]
    return {
        "id": row[0],
        "couple_space_id": row[1],
        "title": row[2],
        "content": row[3],
        "memory_date": str(row[4]) if row[4] else None,
        "place_name": row[5],
        "latitude": row[6],
        "longitude": row[7],
        "cover_photo_url": row[8],
        "visibility": row[9],
        "created_by": row[10],
        "created_at": str(row[11]),
        "updated_at": str(row[12]),
        "archived": row[13],
        "photo_count": row[14],
        "tags": tags,
    }


def _memory_space_for_user(cur: Any, memory_id: int, user_id: int) -> int:
    cur.execute(
        """
        SELECT m.couple_space_id
        FROM memories m
        JOIN couple_members cm ON cm.couple_space_id = m.couple_space_id
        WHERE m.id = %s AND cm.user_id = %s
        LIMIT 1
        """,
        (memory_id, user_id),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return int(row[0])


def _immich_asset_payload(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "couple_space_id": row[1],
        "memory_id": row[2],
        "provider": row[3],
        "immich_asset_id": row[4],
        "immich_album_id": row[5],
        "original_filename": row[6],
        "taken_at": str(row[7]) if row[7] else None,
        "latitude": row[8],
        "longitude": row[9],
        "thumbnail_cache_key": row[10],
        "sort_order": row[11],
        "selected_by": row[12],
        "created_at": str(row[13]),
        "updated_at": str(row[14]),
    }


@router.post("/v1/memories/{memory_id}/immich-assets")
def bind_memory_immich_asset(
    memory_id: int,
    body: ImmichAssetBindRequest,
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    asset_ids = [asset_id.strip() for asset_id in body.asset_ids if asset_id and asset_id.strip()]
    if asset_ids:
        items = [
            _bind_memory_immich_asset(
                memory_id,
                body.model_copy(update={"asset_ids": [], "immich_asset_id": asset_id}),
                user,
            )
            for asset_id in dict.fromkeys(asset_ids)
        ]
        return {"items": items}
    return _bind_memory_immich_asset(memory_id, body, user)


def _bind_memory_immich_asset(
    memory_id: int,
    body: ImmichAssetBindRequest,
    user: dict[str, Any],
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    if not body.immich_asset_id and not body.immich_album_id:
        raise HTTPException(status_code=400, detail="immich_asset_id or immich_album_id is required")

    from app.immich_client import ImmichClient, load_immich_config

    config = load_immich_config()
    original_filename = body.original_filename
    taken_at = body.taken_at
    latitude = body.latitude
    longitude = body.longitude

    if config.enabled and body.immich_asset_id:
        asset = ImmichClient(config).get_asset(body.immich_asset_id)
        original_filename = original_filename or asset.original_filename
        taken_at = taken_at or asset.taken_at
        latitude = latitude if latitude is not None else asset.latitude
        longitude = longitude if longitude is not None else asset.longitude
    elif not original_filename:
        raise HTTPException(status_code=503, detail="Immich integration disabled")

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            space_id = _memory_space_for_user(cur, memory_id, user_id)
            cur.execute(
                """
                INSERT INTO couple_memory_assets(
                    couple_space_id, memory_id, provider, immich_asset_id, immich_album_id,
                    original_filename, taken_at, latitude, longitude, thumbnail_cache_key,
                    sort_order, selected_by
                )
                VALUES (%s, %s, 'immich', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (memory_id, provider, immich_asset_id) WHERE immich_asset_id IS NOT NULL
                DO UPDATE SET
                    original_filename = COALESCE(EXCLUDED.original_filename, couple_memory_assets.original_filename),
                    taken_at = COALESCE(EXCLUDED.taken_at, couple_memory_assets.taken_at),
                    latitude = COALESCE(EXCLUDED.latitude, couple_memory_assets.latitude),
                    longitude = COALESCE(EXCLUDED.longitude, couple_memory_assets.longitude),
                    thumbnail_cache_key = COALESCE(EXCLUDED.thumbnail_cache_key, couple_memory_assets.thumbnail_cache_key),
                    sort_order = EXCLUDED.sort_order,
                    selected_by = EXCLUDED.selected_by,
                    updated_at = NOW()
                RETURNING id, couple_space_id, memory_id, provider, immich_asset_id, immich_album_id,
                          original_filename, taken_at, latitude, longitude, thumbnail_cache_key,
                          sort_order, selected_by, created_at, updated_at
                """,
                (
                    space_id,
                    memory_id,
                    body.immich_asset_id,
                    body.immich_album_id,
                    original_filename,
                    taken_at,
                    latitude,
                    longitude,
                    body.thumbnail_cache_key,
                    body.sort_order,
                    user_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return _immich_asset_payload(row)


@router.get("/v1/memories/{memory_id}/immich-assets")
def list_memory_immich_assets(
    memory_id: int,
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            _memory_space_for_user(cur, memory_id, user_id)
            cur.execute(
                """
                SELECT id, couple_space_id, memory_id, provider, immich_asset_id, immich_album_id,
                       original_filename, taken_at, latitude, longitude, thumbnail_cache_key,
                       sort_order, selected_by, created_at, updated_at
                FROM couple_memory_assets
                WHERE memory_id = %s
                ORDER BY sort_order ASC, id ASC
                """,
                (memory_id,),
            )
            rows = cur.fetchall()
    return {"items": [_immich_asset_payload(row) for row in rows]}


@router.delete("/v1/memories/{memory_id}/immich-assets/{binding_id}")
def delete_memory_immich_asset(
    memory_id: int,
    binding_id: int,
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, str]:
    user_id = _require_couple_user(user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            _memory_space_for_user(cur, memory_id, user_id)
            cur.execute(
                """
                DELETE FROM couple_memory_assets
                WHERE id = %s AND memory_id = %s
                RETURNING id
                """,
                (binding_id, memory_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    return {"status": "ok"}


@router.put("/v1/memories/{memory_id}")
def update_memory(memory_id: int, body: MemoryUpsertRequest, user: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    user_id = _require_couple_user(user)
    if body.visibility not in {"private", "shareable"}:
        raise HTTPException(status_code=400, detail="invalid visibility")
    memory_date = _parse_date_or_none(body.memory_date)

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.couple_space_id
                FROM memories m
                JOIN couple_members cm ON cm.couple_space_id = m.couple_space_id
                WHERE m.id = %s AND cm.user_id = %s
                LIMIT 1
                """,
                (memory_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
            cur.execute(
                """
                UPDATE memories
                SET title=%s, content=%s, memory_date=%s, place_name=%s, latitude=%s, longitude=%s,
                    cover_photo_url=%s, visibility=%s, updated_at=NOW()
                WHERE id=%s
                """,
                (
                    body.title,
                    body.content,
                    memory_date,
                    body.place_name,
                    body.latitude,
                    body.longitude,
                    body.cover_photo_url,
                    body.visibility,
                    memory_id,
                ),
            )
            cur.execute("DELETE FROM memory_tags WHERE memory_id = %s", (memory_id,))
            for tag_clean in _normalize_tags(body.tags):
                cur.execute(
                    "INSERT INTO memory_tags(memory_id, tag) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (memory_id, tag_clean),
                )
        conn.commit()
    return {"status": "ok"}


@router.post("/v1/memories/{memory_id}/archive")
def archive_memory(memory_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    return _set_memory_archived(memory_id, True, user)


@router.post("/v1/memories/{memory_id}/unarchive")
def unarchive_memory(memory_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    return _set_memory_archived(memory_id, False, user)


def _set_memory_archived(memory_id: int, archived: bool, user: dict[str, Any]) -> dict[str, str]:
    user_id = _require_couple_user(user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE memories m
                SET archived = %s, updated_at = NOW()
                FROM couple_members cm
                WHERE m.id = %s AND cm.user_id = %s AND cm.couple_space_id = m.couple_space_id
                """,
                (archived, memory_id, user_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    return {"status": "ok"}


@router.delete("/v1/memories/{memory_id}")
def delete_memory(memory_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    user_id = _require_couple_user(user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM memories m
                USING couple_members cm
                WHERE m.id = %s AND cm.user_id = %s AND cm.couple_space_id = m.couple_space_id
                """,
                (memory_id, user_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    return {"status": "ok"}


@router.get("/v1/map/memories")
def map_memories(
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, memory_date, place_name, latitude, longitude, cover_photo_url
                FROM memories
                WHERE couple_space_id = %s AND archived = false AND latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY memory_date DESC NULLS LAST, id DESC
                """,
                (space_id,),
            )
            rows = cur.fetchall()
            memory_ids = [row[0] for row in rows]
            tags_map: dict[int, list[str]] = {mid: [] for mid in memory_ids}
            if memory_ids:
                cur.execute(
                    "SELECT memory_id, tag FROM memory_tags WHERE memory_id = ANY(%s::bigint[]) ORDER BY id",
                    (memory_ids,),
                )
                for mid, tag_value in cur.fetchall():
                    tags_map.setdefault(mid, []).append(tag_value)
    return {
        "items": [
            {
                "id": row[0],
                "title": row[1],
                "memory_date": str(row[2]) if row[2] else None,
                "place_name": row[3],
                "latitude": row[4],
                "longitude": row[5],
                "cover_photo_url": row[6],
                "tags": tags_map.get(row[0], []),
            }
            for row in rows
        ]
    }


@router.post("/v1/photos/upload")
async def upload_photo(
    file: UploadFile = File(...),
    memory_id: int | None = Query(default=None),
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    if memory_id is not None:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM memories
                    WHERE id = %s AND couple_space_id = %s
                    LIMIT 1
                    """,
                    (memory_id, space_id),
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=400, detail="memory_id not in current couple space")
    saved = await photo_storage().save(file)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO photos(
                    couple_space_id, memory_id, original_filename, original_storage_url,
                    thumbnail_url, display_url, storage_driver
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (
                    space_id,
                    memory_id,
                    file.filename,
                    saved["original_storage_url"],
                    saved["thumbnail_url"],
                    saved["display_url"],
                    saved["storage_driver"],
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return {
        "id": row[0],
        "created_at": str(row[1]),
        "memory_id": memory_id,
        "display_url": saved["display_url"],
        "thumbnail_url": saved["thumbnail_url"],
        "storage_driver": saved["storage_driver"],
    }


@router.get("/v1/photos")
def list_photos(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    memory_id: int | None = Query(default=None),
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    offset = (page - 1) * page_size
    where = ["p.couple_space_id = %s"]
    params: list[Any] = [space_id]
    if memory_id is not None:
        where.append("p.memory_id = %s")
        params.append(memory_id)
    where_sql = " AND ".join(where)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM photos p WHERE {where_sql}", params)
            total = cur.fetchone()[0]
            cur.execute(
                f"""
                SELECT p.id, p.memory_id, p.original_filename, p.display_url, p.thumbnail_url, p.taken_at, p.created_at,
                       m.title AS memory_title
                FROM photos p
                LEFT JOIN memories m ON m.id = p.memory_id
                WHERE {where_sql}
                ORDER BY COALESCE(p.taken_at, p.created_at) DESC, p.id DESC
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, offset],
            )
            rows = cur.fetchall()
    return {
        "items": [
            {
                "id": row[0],
                "memory_id": row[1],
                "original_filename": row[2],
                "display_url": row[3],
                "thumbnail_url": row[4],
                "taken_at": str(row[5]) if row[5] else None,
                "created_at": str(row[6]),
                "memory_title": row[7],
            }
            for row in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("/v1/photos/content/{key}")
def get_photo_content(key: str) -> Response:
    content = _webdock_photo_request("GET", key)
    mime = _detect_image_mime(content) or "application/octet-stream"
    return Response(
        content=content,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/v1/photos/{photo_id}")
def get_photo(photo_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.memory_id, p.original_filename, p.original_storage_url, p.display_url, p.thumbnail_url,
                       p.storage_driver, p.created_at, m.title AS memory_title
                FROM photos p
                LEFT JOIN memories m ON m.id = p.memory_id
                JOIN couple_members cm ON cm.couple_space_id = p.couple_space_id
                WHERE p.id = %s AND cm.user_id = %s
                LIMIT 1
                """,
                (photo_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
    return {
        "id": row[0],
        "memory_id": row[1],
        "original_filename": row[2],
        "original_storage_url": row[3],
        "display_url": row[4],
        "thumbnail_url": row[5],
        "storage_driver": row[6],
        "created_at": str(row[7]),
        "memory_title": row[8],
    }


@router.delete("/v1/photos/{photo_id}")
def delete_photo(photo_id: int, user: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    user_id = _require_couple_user(user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM photos p
                USING couple_members cm
                WHERE p.id = %s AND cm.user_id = %s AND cm.couple_space_id = p.couple_space_id
                RETURNING p.original_storage_url, p.storage_driver
                """,
                (photo_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    try:
        if row[1] == "local":
            LocalPhotoStorage().delete(row[0])
        elif row[1] == "webdock":
            WebDockPhotoStorage().delete(row[0])
    except Exception:
        pass
    return {"status": "ok"}


@router.get("/v1/couple/space")
def get_couple_space(
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    today = date.today()
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, start_date, theme, cover_image_url
                FROM couple_spaces
                WHERE id = %s
                """,
                (space_id,),
            )
            space = cur.fetchone()
            if not space:
                raise HTTPException(status_code=404, detail="not found")
            cur.execute(
                """
                SELECT u.id, u.display_name, u.username, cm.role
                FROM couple_members cm
                JOIN users u ON u.id = cm.user_id
                WHERE cm.couple_space_id = %s
                ORDER BY cm.role DESC, cm.joined_at ASC
                """,
                (space_id,),
            )
            members = cur.fetchall()
            counts: dict[str, int] = {}
            for key, table in [
                ("memories", "memories"),
                ("photos", "photos"),
                ("anniversaries", "anniversaries"),
                ("bucket_items", "bucket_items"),
            ]:
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE couple_space_id = %s", (space_id,))
                counts[key] = cur.fetchone()[0]
            cur.execute(
                """
                SELECT id, title, date, repeat_type, description
                FROM anniversaries
                WHERE couple_space_id = %s
                """,
                (space_id,),
            )
            anniversary_rows = cur.fetchall()

    next_items = [item for row in anniversary_rows if (item := _anniversary_payload(row, today))]
    next_items.sort(key=lambda item: (item["days_remaining"], item["id"]))
    start_date = space[2]
    return {
        "id": space[0],
        "name": space[1],
        "start_date": str(start_date) if start_date else None,
        "theme": space[3],
        "cover_image_url": space[4],
        "days_together": (today - start_date).days if start_date else None,
        "members": [
            {
                "user_id": row[0],
                "display_name": row[1] or row[2],
                "username": row[2],
                "role": row[3],
            }
            for row in members
        ],
        "next_anniversary": next_items[0] if next_items else None,
        "counts": counts,
    }


@router.patch("/v1/couple/space")
def patch_couple_space(
    body: PatchCoupleSpaceRequest,
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, str]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    if not _is_space_owner(user, user_id, space_id):
        raise HTTPException(status_code=403, detail="permission denied")
    fields: list[str] = []
    params: list[Any] = []
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        fields.append("name = %s")
        params.append(name)
    if body.start_date is not None:
        fields.append("start_date = %s")
        params.append(_parse_date_or_none(body.start_date))
    if body.theme is not None:
        fields.append("theme = %s")
        params.append(body.theme.strip() or None)
    if body.cover_image_url is not None:
        fields.append("cover_image_url = %s")
        params.append(body.cover_image_url.strip() or None)
    if not fields:
        return {"status": "ok"}
    params.append(space_id)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE couple_spaces SET {', '.join(fields)}, updated_at = NOW() WHERE id = %s",
                params,
            )
        conn.commit()
    return {"status": "ok"}


@router.get("/v1/tags")
def list_tags(
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT mt.tag, COUNT(*) AS count
                FROM memory_tags mt
                JOIN memories m ON m.id = mt.memory_id
                WHERE m.couple_space_id = %s
                GROUP BY mt.tag
                ORDER BY count DESC, mt.tag ASC
                """,
                (space_id,),
            )
            rows = cur.fetchall()
    return {"items": [{"tag": row[0], "count": row[1]} for row in rows]}


@router.get("/v1/anniversaries")
def list_anniversaries(
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    today = date.today()
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, date, repeat_type, description
                FROM anniversaries
                WHERE couple_space_id = %s
                ORDER BY date ASC, id ASC
                """,
                (space_id,),
            )
            rows = cur.fetchall()
    items = [item for row in rows if (item := _anniversary_payload(row, today))]
    items.sort(key=lambda item: (item["days_remaining"], item["id"]))
    return {"items": items}


@router.get("/v1/anniversaries/next")
def next_anniversary(
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any] | None:
    items = list_anniversaries(couple_space_id, user)["items"]
    return items[0] if items else None


@router.post("/v1/anniversaries")
def create_anniversary(body: AnniversaryUpsertRequest, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, body.couple_space_id)
    if body.repeat_type not in {"none", "yearly", "monthly"}:
        raise HTTPException(status_code=400, detail="invalid repeat_type")
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO anniversaries(couple_space_id, title, date, repeat_type, description)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (space_id, title, _parse_date_or_none(body.date), body.repeat_type, body.description),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    return {"id": new_id}


@router.put("/v1/anniversaries/{anniversary_id}")
def update_anniversary(
    anniversary_id: int,
    body: AnniversaryUpsertRequest,
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, str]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, body.couple_space_id)
    if body.repeat_type not in {"none", "yearly", "monthly"}:
        raise HTTPException(status_code=400, detail="invalid repeat_type")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE anniversaries
                SET title = %s, date = %s, repeat_type = %s, description = %s, updated_at = NOW()
                WHERE id = %s AND couple_space_id = %s
                """,
                (body.title.strip(), _parse_date_or_none(body.date), body.repeat_type, body.description, anniversary_id, space_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    return {"status": "ok"}


@router.delete("/v1/anniversaries/{anniversary_id}")
def delete_anniversary(
    anniversary_id: int,
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, str]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM anniversaries WHERE id = %s AND couple_space_id = %s", (anniversary_id, space_id))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    return {"status": "ok"}


@router.get("/v1/bucket-items")
def list_bucket_items(
    status: str | None = Query(default=None),
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    where = ["b.couple_space_id = %s"]
    params: list[Any] = [space_id]
    if status:
        if status not in {"want", "planned", "done"}:
            raise HTTPException(status_code=400, detail="invalid status")
        where.append("b.status = %s")
        params.append(status)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT b.id, b.title, b.description, b.status, b.target_date, b.completed_memory_id,
                       m.title AS completed_memory_title
                FROM bucket_items b
                LEFT JOIN memories m ON m.id = b.completed_memory_id
                WHERE {' AND '.join(where)}
                ORDER BY CASE b.status WHEN 'planned' THEN 1 WHEN 'want' THEN 2 ELSE 3 END,
                         b.target_date ASC NULLS LAST, b.id DESC
                """,
                params,
            )
            rows = cur.fetchall()
    return {
        "items": [
            {
                "id": row[0],
                "title": row[1],
                "description": row[2],
                "status": row[3],
                "target_date": str(row[4]) if row[4] else None,
                "completed_memory_id": row[5],
                "completed_memory_title": row[6],
            }
            for row in rows
        ]
    }


def _validate_bucket_body(body: BucketItemUpsertRequest, space_id: int) -> None:
    if body.status not in {"want", "planned", "done"}:
        raise HTTPException(status_code=400, detail="invalid status")
    if body.completed_memory_id is not None:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM memories WHERE id = %s AND couple_space_id = %s LIMIT 1",
                    (body.completed_memory_id, space_id),
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=400, detail="completed_memory_id not in current couple space")


@router.post("/v1/bucket-items")
def create_bucket_item(body: BucketItemUpsertRequest, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, body.couple_space_id)
    _validate_bucket_body(body, space_id)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bucket_items(couple_space_id, title, description, status, target_date, completed_memory_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    space_id,
                    title,
                    body.description,
                    body.status,
                    _parse_date_or_none(body.target_date),
                    body.completed_memory_id,
                ),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    return {"id": new_id}


@router.put("/v1/bucket-items/{item_id}")
def update_bucket_item(
    item_id: int,
    body: BucketItemUpsertRequest,
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, str]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, body.couple_space_id)
    _validate_bucket_body(body, space_id)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bucket_items
                SET title = %s, description = %s, status = %s, target_date = %s,
                    completed_memory_id = %s, updated_at = NOW()
                WHERE id = %s AND couple_space_id = %s
                """,
                (
                    body.title.strip(),
                    body.description,
                    body.status,
                    _parse_date_or_none(body.target_date),
                    body.completed_memory_id,
                    item_id,
                    space_id,
                ),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    return {"status": "ok"}


@router.delete("/v1/bucket-items/{item_id}")
def delete_bucket_item(
    item_id: int,
    couple_space_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, str]:
    user_id = _require_couple_user(user)
    space_id = _resolve_couple_space_id(user_id, couple_space_id)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bucket_items WHERE id = %s AND couple_space_id = %s", (item_id, space_id))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    return {"status": "ok"}


@router.post("/v1/memories/{memory_id}/share")
def create_memory_share(
    memory_id: int,
    body: CreateShareLinkRequest,
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    user_id = _require_couple_user(user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.couple_space_id
                FROM memories m
                JOIN couple_members cm ON cm.couple_space_id = m.couple_space_id
                WHERE m.id = %s AND cm.user_id = %s
                LIMIT 1
                """,
                (memory_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
            token = secrets.token_urlsafe(24)
            expires_at = None
            if body.expires_in_days:
                expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
            cur.execute(
                """
                INSERT INTO share_links(couple_space_id, memory_id, token, expires_at)
                VALUES (%s, %s, %s, %s)
                """,
                (row[0], memory_id, token, expires_at),
            )
            cur.execute("UPDATE memories SET visibility = 'shareable', updated_at = NOW() WHERE id = %s", (memory_id,))
        conn.commit()
    url = f"{_share_base_url()}/s/{token}"
    return {"token": token, "url": url, "expires_at": expires_at.isoformat() if expires_at else None}


@router.delete("/v1/share/{token}")
def revoke_share_link(token: str, user: dict[str, Any] = Depends(require_login)) -> dict[str, str]:
    user_id = _require_couple_user(user)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE share_links sl
                SET revoked_at = NOW()
                FROM couple_members cm
                WHERE sl.token = %s AND cm.user_id = %s AND cm.couple_space_id = sl.couple_space_id
                """,
                (token, user_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()
    return {"status": "ok"}


@router.get("/v1/share/{token}")
def get_shared_memory(token: str) -> dict[str, Any]:
    _check_share_rate(token)
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id, m.title, m.content, m.memory_date, m.place_name, m.latitude, m.longitude,
                       m.cover_photo_url, m.visibility, sl.expires_at, sl.revoked_at
                FROM share_links sl
                JOIN memories m ON m.id = sl.memory_id
                WHERE sl.token = %s
                LIMIT 1
                """,
                (token,),
            )
            row = cur.fetchone()
            if not row or row[8] != "shareable" or row[10] is not None:
                raise HTTPException(status_code=404, detail="not found")
            if row[9] is not None:
                now = datetime.now(row[9].tzinfo) if row[9].tzinfo else datetime.now()
                if row[9] <= now:
                    raise HTTPException(status_code=404, detail="not found")
            cur.execute(
                """
                SELECT id, display_url, thumbnail_url, original_filename, taken_at
                FROM photos
                WHERE memory_id = %s
                ORDER BY COALESCE(taken_at, created_at) ASC, id ASC
                """,
                (row[0],),
            )
            photos = cur.fetchall()
            cur.execute(
                """
                SELECT id, couple_space_id, memory_id, provider, immich_asset_id, immich_album_id,
                       original_filename, taken_at, latitude, longitude, thumbnail_cache_key,
                       sort_order, selected_by, created_at, updated_at
                FROM couple_memory_assets
                WHERE memory_id = %s
                ORDER BY sort_order ASC, id ASC
                """,
                (row[0],),
            )
            immich_assets = cur.fetchall()
            cur.execute("SELECT tag FROM memory_tags WHERE memory_id = %s ORDER BY id", (row[0],))
            tags = [r[0] for r in cur.fetchall()]
    return {
        "memory": {
            "id": row[0],
            "title": row[1],
            "content": row[2],
            "memory_date": str(row[3]) if row[3] else None,
            "place_name": row[4],
            "latitude": row[5],
            "longitude": row[6],
            "cover_photo_url": row[7],
            "tags": tags,
        },
        "photos": [
            {
                "id": p[0],
                "display_url": p[1],
                "thumbnail_url": p[2],
                "original_filename": p[3],
                "taken_at": str(p[4]) if p[4] else None,
            }
            for p in photos
        ],
        "immich_assets": [_immich_asset_payload(r) for r in immich_assets],
    }


@router.get("/v1/admin/couple-spaces")
def admin_couple_spaces(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, start_date, theme, created_at FROM couple_spaces ORDER BY id DESC")
            spaces = cur.fetchall()
            cur.execute(
                """
                SELECT cm.couple_space_id, u.id, u.username, u.display_name, cm.role, cm.joined_at
                FROM couple_members cm
                JOIN users u ON u.id = cm.user_id
                ORDER BY cm.couple_space_id DESC, cm.id ASC
                """
            )
            member_rows = cur.fetchall()

    members_map: dict[int, list[dict[str, Any]]] = {}
    for row in member_rows:
        members_map.setdefault(row[0], []).append(
            {
                "user_id": row[1],
                "username": row[2],
                "display_name": row[3],
                "role": row[4],
                "joined_at": str(row[5]),
            }
        )

    return {
        "items": [
            {
                "id": row[0],
                "name": row[1],
                "start_date": str(row[2]) if row[2] else None,
                "theme": row[3],
                "created_at": str(row[4]),
                "members": members_map.get(row[0], []),
            }
            for row in spaces
        ]
    }


@router.post("/v1/admin/couple-spaces")
def admin_create_couple_space(
    body: CreateCoupleSpaceRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO couple_spaces(name, start_date, theme) VALUES (%s, %s, %s) RETURNING id",
                (name, body.start_date, body.theme),
            )
            new_id = cur.fetchone()[0]
        conn.commit()

    _audit(actor.get("sub"), "admin.couple_spaces.create", "couple_spaces", str(new_id), {"name": name})
    return {"id": new_id}


@router.post("/v1/admin/couple-spaces/{space_id}/members")
def admin_add_couple_member(
    space_id: int,
    body: AddCoupleMemberRequest,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    role = body.role.strip() or "member"
    if role not in {"member", "owner"}:
        raise HTTPException(status_code=400, detail="invalid role")

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM couple_spaces WHERE id = %s", (space_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="couple space not found")
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            user_row = cur.fetchone()
            if not user_row:
                raise HTTPException(status_code=404, detail="user not found")
            cur.execute(
                """
                INSERT INTO couple_members(couple_space_id, user_id, role)
                VALUES (%s, %s, %s)
                ON CONFLICT(couple_space_id, user_id)
                DO UPDATE SET role = EXCLUDED.role
                """,
                (space_id, user_row[0], role),
            )
        conn.commit()

    _audit(actor.get("sub"), "admin.couple_spaces.add_member", "couple_spaces", str(space_id), {"username": username, "role": role})
    return {"status": "ok"}


@router.delete("/v1/admin/couple-spaces/{space_id}/members/{user_id}")
def admin_remove_couple_member(
    space_id: int,
    user_id: int,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM couple_members WHERE couple_space_id = %s AND user_id = %s",
                (space_id, user_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="member not found")
        conn.commit()

    _audit(actor.get("sub"), "admin.couple_spaces.remove_member", "couple_spaces", str(space_id), {"user_id": user_id})
    return {"status": "ok"}


@router.post("/v1/admin/users/{user_id}/grant-couple-access")
def admin_grant_couple_access(
    user_id: int,
    actor: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="user not found")
            cur.execute(
                """
                INSERT INTO permissions(code, name, description)
                VALUES ('couple_memory_access', 'Couple Memory 访问', '访问双人私密回忆空间')
                ON CONFLICT(code) DO NOTHING
                """
            )
            cur.execute(
                """
                INSERT INTO roles(code, name, description)
                VALUES ('couple_memory', 'Couple Memory 成员', '可访问 Couple Memory 私密空间')
                ON CONFLICT(code) DO NOTHING
                """
            )
            cur.execute(
                """
                INSERT INTO role_permissions(role_id, permission_id)
                SELECT r.id, p.id
                FROM roles r, permissions p
                WHERE r.code = 'couple_memory' AND p.code = 'couple_memory_access'
                ON CONFLICT DO NOTHING
                """
            )
            cur.execute(
                """
                INSERT INTO user_roles(user_id, role_id)
                SELECT %s, id FROM roles WHERE code = 'couple_memory'
                ON CONFLICT DO NOTHING
                """,
                (user_id,),
            )
        conn.commit()

    _audit(actor.get("sub"), "admin.users.grant_couple_access", "users", str(user_id))
    return {"status": "ok"}
