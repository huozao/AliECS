from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import requests

from app.pipelines.backfill_smartsheet_images import _field_id_by_title, _field_list, _sheet_id_by_title
from app.pipelines.group_message_listener import (
    DEFAULT_DOCID,
    _default_smartsheet_factory,
    resolve_groupbot_profile,
)
from app.storage.postgres import close_store, open_store

RND_SHEET_TITLE = "研发过程记录"
# 第一列复用建表自带的默认文本字段（重命名为「时间」），其余字段追加。
RND_FIELDS: list[tuple[str, str]] = [
    ("时间", "FIELD_TYPE_TEXT"),
    ("发言人", "FIELD_TYPE_TEXT"),
    ("节点类型", "FIELD_TYPE_TEXT"),
    ("内容", "FIELD_TYPE_TEXT"),
    ("审批单编号", "FIELD_TYPE_TEXT"),
    ("图片", "FIELD_TYPE_IMAGE"),
]


def ensure_rnd_sheet(client: Any, docid: str) -> str:
    """确保「研发过程记录」子表存在（不存在则建表+建字段）。返回 sheet_id。"""
    sheet_id = _sheet_id_by_title(client.get_sheets(docid), RND_SHEET_TITLE)
    if sheet_id:
        return sheet_id
    client.add_sheet(docid, RND_SHEET_TITLE)
    sheet_id = _sheet_id_by_title(client.get_sheets(docid), RND_SHEET_TITLE)
    if not sheet_id:
        raise RuntimeError("建「研发过程记录」子表后仍找不到 sheet_id。")

    first_title, first_type = RND_FIELDS[0]
    existing = _field_list(client.get_fields(docid, sheet_id))
    if existing:
        default_fid = str(existing[0].get("field_id") or existing[0].get("id") or "").strip()
        if default_fid:
            client.update_fields(
                docid, sheet_id, [{"field_id": default_fid, "field_title": first_title, "field_type": first_type}]
            )
        to_add = RND_FIELDS[1:]
    else:
        to_add = RND_FIELDS
    if to_add:
        client.add_fields(docid, sheet_id, [{"field_title": t, "field_type": ft} for t, ft in to_add])
    return sheet_id


def _fmt_time(value: Any) -> str:
    if value is None:
        return ""
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(value)


def _quote_image_urls(quote: Any) -> list[str]:
    if not isinstance(quote, dict):
        return []
    image = quote.get("image")
    if isinstance(image, dict):
        url = str(image.get("url") or "").strip()
        return [url] if url else []
    return []


def _local_image_bytes(paths: Any) -> list[bytes]:
    if not isinstance(paths, list):
        return []
    root = Path(os.getenv("WECOM_GROUP_MEDIA_DIR", "/app/wecom-group-media")).resolve()
    result: list[bytes] = []
    for value in paths:
        try:
            path = Path(str(value)).resolve()
            path.relative_to(root)
            size = path.stat().st_size
            if 0 < size <= 20 * 1024 * 1024:
                result.append(path.read_bytes())
        except (OSError, ValueError):
            continue
    return result


def build_node_row_values(msg: dict[str, Any], requirement_key: str, image_url: str = "") -> dict[str, Any]:
    content = str(msg.get("node_summary") or "").strip() or str(msg.get("text_content") or "").strip()
    values: dict[str, Any] = {
        "时间": [{"type": "text", "text": _fmt_time(msg.get("created_at"))}],
        "发言人": [{"type": "text", "text": str(msg.get("from_userid") or "")}],
        "节点类型": [{"type": "text", "text": str(msg.get("node_category") or "")}],
        "内容": [{"type": "text", "text": content}],
        "审批单编号": [{"type": "text", "text": requirement_key}],
    }
    if image_url:
        values["图片"] = [{"image_url": image_url, "title": "群图片.jpg"}]
    return values


def run_write_rnd_records(
    profiles_arg: str = "",
    *,
    store: Any | None = None,
    smartsheet_factory: Callable[[str], Any] = _default_smartsheet_factory,
    docid: str = DEFAULT_DOCID,
    limit: int = 50,
) -> int:
    """把已标节点、有归属、未写表的群消息写入「研发过程记录」子表。返回写入条数。"""
    profile = resolve_groupbot_profile(profiles_arg)
    if not profile:
        return 0
    owned_store = store is None
    store = store or open_store()
    written = 0
    try:
        pending = store.list_pending_node_messages(limit=limit)
        if not pending:
            return 0
        client = smartsheet_factory(profile)
        sheet_id = ensure_rnd_sheet(client, docid)
        for msg in pending:
            try:
                binding = store.get_group_binding(msg["chatid"]) or {}
                requirement_key = str(binding.get("requirement_key") or "")
                image_url = ""
                for content in _local_image_bytes(msg.get("media_paths")):
                    try:
                        image_url = client.upload_image(docid, content)
                        break
                    except Exception as exc:  # noqa: BLE001 - 图片失败不挡文本入表
                        print(f"[研发记录] 本地图片上传失败（继续尝试引用图片）：{exc}")
                if image_url:
                    urls: list[str] = []
                else:
                    urls = _quote_image_urls(msg.get("quote_json"))
                for url in urls:
                    try:
                        content = requests.get(url, timeout=20).content
                        image_url = client.upload_image(docid, content)
                        break
                    except Exception as exc:  # noqa: BLE001 - 图片失败不挡文本入表
                        print(f"[研发记录] 图片下载/上传失败（跳过图片）：{exc}")
                values = build_node_row_values(msg, requirement_key, image_url)
                client.add_records(docid, sheet_id, [{"values": values}])
                store.mark_message_written(msg["msgid"])
                written += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮其余
                print(f"[研发记录] 写入失败 msgid={msg.get('msgid')}：{exc}")
    finally:
        if owned_store:
            close_store(store)
    return written
