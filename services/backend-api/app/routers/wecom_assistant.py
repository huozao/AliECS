"""OpenClaw 企微助手的内部业务入口。

这里只做确定性命令、消息持久化和 AI 草稿状态管理；真正的模型调用由
OpenClaw -> openclaw-bridge -> WebDock 完成，企微写表仍由 doc-sync-worker 执行。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from app.core import _conn


router = APIRouter(prefix="/v1/internal/wecom", tags=["wecom-internal"])

DEFAULT_SHEET_TITLE = "配色&样品需求单"
_NODE_COMMAND_RE = re.compile(r"#节点(?:\s+([^\s]{1,8}))?(?:\s+(.+))?", re.DOTALL)
_AI_NODE_COMMAND_RE = re.compile(r"#AI节点(?:\s+([^\s]{1,8}))?(?:\s+(.+))?", re.IGNORECASE | re.DOTALL)
_DATA_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|jpg|gif|webp));base64,([A-Za-z0-9+/=\r\n]+)$", re.IGNORECASE)
_MIME_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


class InboundMessage(BaseModel):
    msgid: str = Field(min_length=1, max_length=256)
    account_id: str = Field(default="company-b", max_length=128)
    chatid: str = Field(default="", max_length=512)
    chattype: str = Field(default="private", max_length=32)
    from_userid: str = Field(default="", max_length=256)
    text_content: str = Field(default="", max_length=20000)
    images: list[str] = Field(default_factory=list, max_length=20)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class AiResult(BaseModel):
    draft_msgid: str = Field(min_length=1, max_length=256)
    result_text: str = Field(min_length=1, max_length=50000)


def _require_internal_token(value: str | None) -> None:
    expected = os.getenv("OPENCLAW_INTERNAL_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="OPENCLAW_INTERNAL_TOKEN is not configured")
    if not value or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="invalid internal token")


def _first_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        for item in value:
            text = _first_text(item)
            if text:
                return text
    if isinstance(value, dict):
        for key in ("text", "value", "url", "link"):
            text = _first_text(value.get(key))
            if text:
                return text
    return ""


def _approval_from_link(value: Any) -> str:
    text = _first_text(value)
    if not text:
        return ""
    try:
        query = parse_qs(urlparse(text).query)
    except ValueError:
        return ""
    for key in ("sp_no", "spNo", "approval_no"):
        if query.get(key):
            return str(query[key][0]).strip()
    return ""


def _requirement_source(cur: Any) -> tuple[str, str]:
    title = os.getenv("WECOM_RND_SOURCE_SHEET", DEFAULT_SHEET_TITLE).strip() or DEFAULT_SHEET_TITLE
    configured = os.getenv("WECOM_RND_DOCID", "").strip()
    if configured:
        return (configured, title) if configured.startswith("dc") and len(configured) >= 80 else ("", title)
    cur.execute(
        """
        SELECT DISTINCT external_doc_id
        FROM external_sources
        WHERE provider='wecom' AND env_profile='COMPANY_B'
          AND source_type='smartsheet_sheet' AND status='active'
          AND sheet_name=%s AND external_doc_id LIKE 'dc%%'
          AND length(external_doc_id) >= 80
        """,
        (title,),
    )
    rows = cur.fetchall()
    return (str(rows[0][0]), title) if len(rows) == 1 else ("", title)


def _requirement_index(cur: Any) -> dict[str, dict[str, str]]:
    docid, title = _requirement_source(cur)
    if not docid:
        return {}
    cur.execute(
        """
        SELECT er.external_record_id, er.normalized_json
        FROM external_sources es
        JOIN external_records er ON er.source_id = es.id
        WHERE es.provider = 'wecom'
          AND es.env_profile = 'COMPANY_B'
          AND es.external_doc_id = %s
          AND es.sheet_name = %s
          AND es.status = 'active'
        ORDER BY er.id
        """,
        (docid, title),
    )
    result: dict[str, dict[str, str]] = {}
    for record_id, normalized in cur.fetchall():
        values = normalized if isinstance(normalized, dict) else {}
        codes = [_first_text(values.get("审批单编号")), _approval_from_link(values.get("审批链接"))]
        for code in codes:
            if code:
                result.setdefault(code, {"record_id": str(record_id), "requirement_key": code, "docid": docid, "sheet_title": title})
    return result


def _parse_node_command(text: str, *, ai: bool = False) -> tuple[bool, str, str]:
    match = (_AI_NODE_COMMAND_RE if ai else _NODE_COMMAND_RE).search(text or "")
    if not match:
        return False, "", ""
    category = str(match.group(1) or "").strip()
    summary = str(match.group(2) or "").strip()
    return True, category, summary


def _media_dir() -> Path:
    root = Path(os.getenv("WECOM_GROUP_MEDIA_DIR", "/app/wecom-group-media")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _save_images(msgid: str, images: list[str]) -> list[str]:
    saved: list[str] = []
    root = _media_dir()
    safe_msgid = hashlib.sha256(msgid.encode("utf-8")).hexdigest()[:24]
    for position, data_url in enumerate(images):
        match = _DATA_URL_RE.match(str(data_url or ""))
        if not match:
            continue
        mime = match.group(1).lower()
        try:
            content = base64.b64decode(match.group(2), validate=True)
        except ValueError:
            continue
        if not content or len(content) > _MAX_IMAGE_BYTES:
            continue
        digest = hashlib.sha256(content).hexdigest()[:16]
        path = root / f"{safe_msgid}-{position}-{digest}{_MIME_SUFFIX[mime]}"
        if not path.exists():
            path.write_bytes(content)
        saved.append(str(path))
    return saved


def _insert_message(cur: Any, payload: InboundMessage, media_paths: list[str], record_id: str) -> bool:
    cur.execute(
        """
        INSERT INTO group_messages(
            msgid, chatid, from_userid, msgtype, text_content,
            quote_json, media_paths, record_id, raw_json
        ) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, %s)
        ON CONFLICT(msgid) DO NOTHING
        RETURNING id
        """,
        (
            payload.msgid,
            payload.chatid,
            payload.from_userid,
            "mixed" if media_paths and payload.text_content else ("image" if media_paths else "text"),
            payload.text_content,
            Jsonb(media_paths),
            record_id,
            Jsonb({"source": "openclaw", "account_id": payload.account_id, "metadata": payload.raw_metadata}),
        ),
    )
    return cur.fetchone() is not None


def _latest_draft(cur: Any, payload: InboundMessage, status: str) -> tuple[Any, ...] | None:
    cur.execute(
        """
        SELECT id, source_msgid, node_category, result_text
        FROM wecom_ai_drafts
        WHERE chatid = %s AND from_userid = %s AND status = %s
        ORDER BY id DESC LIMIT 1
        FOR UPDATE
        """,
        (payload.chatid, payload.from_userid, status),
    )
    return cur.fetchone()


@router.post("/inbound")
def wecom_inbound(payload: InboundMessage, x_internal_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_internal_token(x_internal_token)
    if payload.chattype.lower() != "group" or not payload.chatid:
        return {"action": "continue", "reply": ""}

    text = payload.text_content.strip()
    explicit_bind = "#绑定" in text
    is_ai_node, ai_category, ai_question = _parse_node_command(text, ai=True)
    is_node, node_category, node_summary = _parse_node_command(text)
    # 普通图片问答只由 WebDock 临时处理；仅写表候选图片进入持久卷。
    media_paths = _save_images(payload.msgid, payload.images) if (is_ai_node or is_node) else []

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            if text.startswith("#确认节点"):
                draft = _latest_draft(cur, payload, "ready")
                if not draft:
                    return {"action": "reply", "reply": "没有待确认的 AI 节点草稿。"}
                draft_id, source_msgid, category, result_text = draft
                cur.execute(
                    "UPDATE group_messages SET is_node=TRUE, node_category=%s, node_summary=%s WHERE msgid=%s",
                    (category, result_text, source_msgid),
                )
                cur.execute(
                    "UPDATE wecom_ai_drafts SET status='confirmed', confirmed_by=%s, confirmed_at=NOW(), updated_at=NOW() WHERE id=%s",
                    (payload.from_userid, draft_id),
                )
                conn.commit()
                return {"action": "reply", "reply": "✅ AI 草稿已确认，已进入研发过程写表队列。"}

            if text.startswith("#取消节点"):
                draft = _latest_draft(cur, payload, "ready")
                if not draft:
                    return {"action": "reply", "reply": "没有待取消的 AI 节点草稿。"}
                cur.execute("UPDATE wecom_ai_drafts SET status='cancelled', updated_at=NOW() WHERE id=%s", (draft[0],))
                conn.commit()
                return {"action": "reply", "reply": "已取消该 AI 节点草稿，不会写表。"}

            cur.execute(
                "SELECT record_id, requirement_key FROM group_record_map WHERE chatid=%s",
                (payload.chatid,),
            )
            binding = cur.fetchone()

            if binding is None or explicit_bind:
                index = _requirement_index(cur)
                matches = [code for code in index if code and code in text]
                if len(matches) == 1:
                    code = matches[0]
                    record = index[code]
                    cur.execute(
                        """
                        INSERT INTO group_record_map(
                            provider, env_profile, chatid, external_doc_id, sheet_title,
                            record_id, requirement_key, bound_by, bound_at, updated_at
                        ) VALUES ('wecom', 'COMPANY_B', %s, %s, %s, %s, %s, %s, NOW(), NOW())
                        ON CONFLICT(chatid) DO UPDATE SET
                            record_id=EXCLUDED.record_id, requirement_key=EXCLUDED.requirement_key,
                            bound_by=EXCLUDED.bound_by, updated_at=NOW()
                        """,
                        (payload.chatid, record["docid"], record["sheet_title"], record["record_id"], code, payload.from_userid),
                    )
                    cur.execute(
                        "UPDATE group_messages SET record_id=%s WHERE chatid=%s AND COALESCE(record_id, '')=''",
                        (record["record_id"], payload.chatid),
                    )
                    filled = int(cur.rowcount or 0)
                    conn.commit()
                    return {"action": "reply", "reply": f"✅ 已关联审批单编号 {code}（回填 {filled} 条历史消息）"}
                if len(matches) > 1:
                    return {"action": "reply", "reply": "识别到多个审批单编号：" + "、".join(matches[:5]) + "，请只保留一个再发我"}
                if explicit_bind:
                    return {"action": "reply", "reply": "未识别到有效审批单编号，请发送 #绑定 审批单编号。"}

            record_id = str(binding[0]) if binding else ""
            inserted = _insert_message(cur, payload, media_paths, record_id)

            if is_ai_node and inserted:
                if not binding:
                    conn.commit()
                    return {"action": "reply", "reply": "已保存消息，但本群尚未关联需求；请先发送 #绑定 审批单编号。"}
                prompt = ai_question or "请识别图片及上下文，并整理成可写入研发过程记录的简洁事实摘要。"
                cur.execute(
                    """
                    INSERT INTO wecom_ai_drafts(source_msgid, chatid, from_userid, node_category, question, status)
                    VALUES (%s, %s, %s, %s, %s, 'analyzing')
                    ON CONFLICT(source_msgid) DO NOTHING
                    """,
                    (payload.msgid, payload.chatid, payload.from_userid, ai_category, prompt),
                )
                conn.commit()
                return {
                    "action": "ai_draft",
                    "draft_msgid": payload.msgid,
                    "ai_prompt": "请只输出可核验的研发过程事实摘要，不要虚构；看不清处明确标注。\n用户要求：" + prompt,
                }

            if is_node and inserted:
                cur.execute(
                    "UPDATE group_messages SET is_node=TRUE, node_category=%s, node_summary=%s WHERE msgid=%s",
                    (node_category, node_summary, payload.msgid),
                )
                conn.commit()
                if not binding:
                    return {"action": "reply", "reply": "已保存节点，但本群尚未关联需求；请发送 #绑定 审批单编号 后再写表。"}
                return {"action": "reply", "reply": "✅ 已标记为研发节点，已进入写表队列。"}

            conn.commit()
            if binding is None:
                # 普通群问答不依赖研发需求绑定；只有写表类命令需要先绑定。
                return {"action": "continue", "reply": ""}
            return {"action": "continue", "reply": ""}


@router.post("/ai-result")
def wecom_ai_result(payload: AiResult, x_internal_token: str | None = Header(default=None)) -> dict[str, str]:
    _require_internal_token(x_internal_token)
    result = payload.result_text.strip()
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wecom_ai_drafts
                SET result_text=%s, status='ready', updated_at=NOW()
                WHERE source_msgid=%s AND status='analyzing'
                RETURNING id
                """,
                (result, payload.draft_msgid),
            )
            updated = cur.fetchone()
        conn.commit()
    if not updated:
        raise HTTPException(status_code=409, detail="draft is not analyzing")
    return {"reply": f"AI 节点草稿：\n{result}\n\n确认写入请回复 #确认节点；放弃请回复 #取消节点。"}
