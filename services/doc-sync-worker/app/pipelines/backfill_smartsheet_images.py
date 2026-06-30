from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from app.providers.wecom import (
    WeComSmartsheetClient,
    credentials_for_profile,
    env_profiles,
    get_profiled_env,
)
from app.providers.wecom_approval import WeComApprovalClient
from app.storage.postgres import close_store, open_store, record_values


SKIPPED_STATUSES = {"done", "no_image"}


@dataclass(frozen=True)
class BackfillImage:
    title: str
    content: bytes


@dataclass(frozen=True)
class BackfillResult:
    exit_code: int
    target_count: int = 0
    scanned_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    error_count: int = 0


def parse_sp_no_from_link(link: str) -> str:
    query = dict(parse_qsl(urlsplit(str(link or "")).query, keep_blank_values=True))
    return str(query.get("sp_no") or "").strip()


def _record_id(record: dict[str, Any]) -> str:
    for key in ("record_id", "id", "recordId"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _cell_has_value(value: Any) -> bool:
    if value in (None, ""):
        return False
    if isinstance(value, list):
        return any(_cell_has_value(item) for item in value)
    if isinstance(value, dict):
        return any(str(item or "").strip() for item in value.values())
    return bool(str(value).strip())


def _links_from_cell(value: Any) -> list[str]:
    if isinstance(value, list):
        links: list[str] = []
        for item in value:
            links.extend(_links_from_cell(item))
        return links
    if isinstance(value, dict):
        links = []
        for key in ("link", "url", "href"):
            item = str(value.get(key) or "").strip()
            if item:
                links.append(item)
        return links
    text = str(value or "").strip()
    return [text] if text.startswith(("http://", "https://")) else []


def _value_for_field(values: dict[str, Any], field_id: str, field_title: str = "") -> Any:
    if field_id in values:
        return values.get(field_id)
    if field_title and field_title in values:
        return values.get(field_title)
    return None


def _field_key_for_write(values: dict[str, Any], field_id: str, field_title: str) -> str:
    if field_title:
        return field_title
    return field_id


def backfill_candidate(
    record: dict[str, Any],
    attachment_field_id: str,
    image_field_id: str,
    *,
    attachment_title: str = "",
    image_title: str = "",
) -> tuple[str, str] | None:
    values = record_values(record)
    if _cell_has_value(_value_for_field(values, image_field_id, image_title)):
        return None
    record_id = _record_id(record)
    if not record_id:
        return None
    for link in _links_from_cell(_value_for_field(values, attachment_field_id, attachment_title)):
        sp_no = parse_sp_no_from_link(link)
        if sp_no:
            return record_id, sp_no
    return None


def _field_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    fields = response.get("fields") or response.get("field_list") or response.get("data") or []
    if isinstance(fields, dict):
        fields = fields.get("fields") or fields.get("field_list") or []
    return fields if isinstance(fields, list) else []


def _field_id_by_title(response: dict[str, Any], title: str) -> str:
    for field in _field_list(response):
        if str(field.get("field_title") or field.get("title") or field.get("name") or "").strip() == title:
            return str(field.get("field_id") or field.get("id") or "").strip()
    return ""


def _sheet_id_by_title(sheets: list[dict[str, Any]], title: str) -> str:
    for sheet in sheets:
        sheet_title = str(sheet.get("title") or sheet.get("name") or sheet.get("sheet_name") or "").strip()
        if sheet_title == title:
            return str(sheet.get("sheet_id") or sheet.get("id") or sheet.get("sheetId") or "").strip()
    return ""


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            try:
                return json.loads(text)
            except ValueError:
                return value
    return value


def _approval_contents(detail: dict[str, Any]) -> list[dict[str, Any]]:
    info = detail.get("info") if isinstance(detail, dict) else {}
    apply_data = info.get("apply_data") if isinstance(info, dict) else {}
    contents = apply_data.get("contents") if isinstance(apply_data, dict) else detail.get("contents", [])
    return contents if isinstance(contents, list) else []


def _image_kind(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    return ""


def _title_from_url(url: str, fallback: str) -> str:
    name = PurePosixPath(urlsplit(url).path).name
    return name or fallback


def _append_image(images: list[BackfillImage], title: str, content: bytes) -> None:
    kind = _image_kind(content)
    if not kind:
        return
    clean_title = str(title or f"image.{kind}").strip() or f"image.{kind}"
    if "." not in PurePosixPath(clean_title).name:
        clean_title = f"{clean_title}.{kind}"
    images.append(BackfillImage(title=clean_title, content=content))


def collect_approval_images(approval_client: Any, detail: dict[str, Any]) -> list[BackfillImage]:
    images: list[BackfillImage] = []
    for control in _approval_contents(detail):
        value = _json_value(control.get("value") if isinstance(control, dict) else None)
        control_type = str(control.get("control") or control.get("type") or "").lower()
        if isinstance(value, dict) and ("file" in control_type or "files" in value):
            files = value.get("files") or value.get("file") or []
            if isinstance(files, dict):
                files = [files]
            for file_item in files if isinstance(files, list) else []:
                if not isinstance(file_item, dict):
                    continue
                file_id = str(file_item.get("file_id") or file_item.get("media_id") or "").strip()
                if not file_id:
                    continue
                title = str(file_item.get("file_name") or file_item.get("name") or file_item.get("title") or file_id).strip()
                _append_image(images, title, approval_client.download_media(file_id))
        if "image" in control_type:
            urls: list[str] = []
            if isinstance(value, list):
                urls = [str(item).strip() for item in value if str(item).strip()]
            elif isinstance(value, dict):
                raw_urls = value.get("images") or value.get("image_urls") or value.get("urls") or []
                if isinstance(raw_urls, str):
                    raw_urls = [raw_urls]
                urls = [str(item).strip() for item in raw_urls if str(item).strip()]
            for index, url in enumerate(urls, start=1):
                _append_image(images, _title_from_url(url, f"approval-image-{index}.jpg"), approval_client.download_url(url))
    return images


def _default_smartsheet_client(profile: str) -> WeComSmartsheetClient:
    credentials = credentials_for_profile(profile)
    if not credentials:
        raise RuntimeError(f"{profile} 缺少 WECOM_{profile}_CORP_ID / APP_SECRET，跳过图片回填。")
    credential = credentials[0]
    return WeComSmartsheetClient(credential.corpid, credential.secret)


def _default_approval_client(profile: str) -> WeComApprovalClient:
    corpid = get_profiled_env("CORP_ID", "WECOM", profile)
    secret = get_profiled_env("APPROVAL_SECRET", "WECOM", profile) or get_profiled_env("APP_SECRET", "WECOM", profile)
    if not corpid or not secret:
        raise RuntimeError(f"{profile} 缺少 WECOM_{profile}_APPROVAL_SECRET 或 APP_SECRET，跳过图片回填。")
    return WeComApprovalClient(corpid, secret)


def run_backfill_images(
    profiles_arg: str = "",
    *,
    store: Any | None = None,
    smartsheet_client_factory: Callable[[str], Any] = _default_smartsheet_client,
    approval_client_factory: Callable[[str], Any] = _default_approval_client,
    dry_run: bool = False,
) -> BackfillResult:
    profiles = env_profiles(profiles_arg)
    owned_store = store is None
    store = store or open_store()
    target_count = scanned_count = updated_count = skipped_count = error_count = 0
    try:
        targets = store.list_image_backfill_targets(profiles)
        target_count = len(targets)
        for target in targets:
            profile = str(target.get("env_profile") or "").strip()
            docid = str(target.get("external_doc_id") or "").strip()
            try:
                sheet_client = smartsheet_client_factory(profile)
                approval_client = approval_client_factory(profile)
                sheet_id = _sheet_id_by_title(sheet_client.get_sheets(docid), str(target.get("sheet_title") or ""))
                if not sheet_id:
                    raise RuntimeError(f"找不到工作表：{target.get('sheet_title')}")
                fields = sheet_client.get_fields(docid, sheet_id)
                attachment_field_id = _field_id_by_title(fields, str(target.get("attachment_field_title") or ""))
                image_field_id = _field_id_by_title(fields, str(target.get("image_field_title") or ""))
                if not attachment_field_id or not image_field_id:
                    raise RuntimeError("找不到附件列或图片列。")
                records = sheet_client.get_records(docid, sheet_id).get("records") or []
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    scanned_count += 1
                    candidate = backfill_candidate(
                        record,
                        attachment_field_id,
                        image_field_id,
                        attachment_title=str(target.get("attachment_field_title") or ""),
                        image_title=str(target.get("image_field_title") or ""),
                    )
                    if not candidate:
                        skipped_count += 1
                        continue
                    record_id, sp_no = candidate
                    status = store.get_image_backfill_status(docid, sheet_id, record_id)
                    if status in SKIPPED_STATUSES:
                        skipped_count += 1
                        continue
                    try:
                        detail = approval_client.get_approval_detail(sp_no)
                        images = collect_approval_images(approval_client, detail)
                        if not images:
                            if not dry_run:
                                store.upsert_image_backfill_log(
                                    provider="wecom",
                                    env_profile=profile,
                                    external_doc_id=docid,
                                    sheet_id=sheet_id,
                                    record_id=record_id,
                                    sp_no=sp_no,
                                    status="no_image",
                                    image_count=0,
                                    error="",
                                )
                            continue
                        payload = [
                            {
                                "record_id": record_id,
                                "values": {
                                    _field_key_for_write(
                                        record_values(record),
                                        image_field_id,
                                        str(target.get("image_field_title") or ""),
                                    ): [
                                        {"image_url": sheet_client.upload_image(docid, image.content), "title": image.title}
                                        for image in images
                                    ]
                                },
                            }
                        ]
                        if not dry_run:
                            sheet_client.update_records(docid, sheet_id, payload)
                            store.upsert_image_backfill_log(
                                provider="wecom",
                                env_profile=profile,
                                external_doc_id=docid,
                                sheet_id=sheet_id,
                                record_id=record_id,
                                sp_no=sp_no,
                                status="done",
                                image_count=len(images),
                                error="",
                            )
                        updated_count += 1
                    except Exception as exc:  # noqa: BLE001
                        error_count += 1
                        if not dry_run:
                            store.upsert_image_backfill_log(
                                provider="wecom",
                                env_profile=profile,
                                external_doc_id=docid,
                                sheet_id=sheet_id,
                                record_id=record_id,
                                sp_no=sp_no,
                                status="error",
                                image_count=0,
                                error=str(exc)[:1000],
                            )
            except Exception as exc:  # noqa: BLE001
                error_count += 1
                print(f"[图片回填] target={target.get('id') or docid} 跳过：{exc}")
    finally:
        if owned_store:
            close_store(store)
    return BackfillResult(
        exit_code=0 if error_count == 0 else 1,
        target_count=target_count,
        scanned_count=scanned_count,
        updated_count=updated_count,
        skipped_count=skipped_count,
        error_count=error_count,
    )


def run_backfill_probe(sp_no: str, profiles_arg: str = "", dry_run: bool = True) -> int:
    profiles = env_profiles(profiles_arg)
    if not profiles:
        print("未配置 WECOM_ENV_PROFILES，也未传入 --profiles。")
        return 1
    approval_client = _default_approval_client(profiles[0])
    detail = approval_client.get_approval_detail(sp_no)
    images = collect_approval_images(approval_client, detail)
    print(f"[图片回填探针] sp_no={sp_no} images={len(images)} dry_run={dry_run}")
    return 0 if images else 1
