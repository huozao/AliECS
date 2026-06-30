from __future__ import annotations

import time
from typing import Any, Callable

from app.pipelines.backfill_smartsheet_images import (
    _field_id_by_title,
    _links_from_cell,
    _record_id,
    _sheet_id_by_title,
    parse_sp_no_from_link,
)
from app.providers.wecom import (
    WeComSmartsheetClient,
    credentials_for_profile,
    env_profiles,
    get_profiled_env,
)
from app.providers.wecom_groupbot import WeComGroupBotClient
from app.storage.postgres import close_store, first_text_cell, open_store, record_values

DEFAULT_DOCID = "dc45aaSDeAwXO54CKSmFkl3ZOH8H_MLVqEmnfE07PONMKTJGB_4T_d5_8LKzdJ7QB2x7lfi8fQkghPaG5gKWyWLA"
DEFAULT_SHEET_TITLE = "配色&样品需求单"
APPROVAL_NO_FIELD = "审批单编号"
APPROVAL_LINK_FIELD = "审批链接"
BIND_KEYWORD = "#绑定"
NODE_KEYWORD = "#节点"


# ---------------- 纯函数（可单测，无 IO） ----------------

def parse_callback(frame: dict[str, Any]) -> dict[str, Any] | None:
    """把 aibot_msg_callback 帧解析成规范化消息；非消息帧返回 None。"""
    if not isinstance(frame, dict) or frame.get("cmd") != "aibot_msg_callback":
        return None
    body = frame.get("body") or {}
    msgid = str(body.get("msgid") or "").strip()
    if not msgid:
        return None
    msgtype = str(body.get("msgtype") or "").strip()
    text = ""
    if msgtype == "text":
        text = str((body.get("text") or {}).get("content") or "")
    return {
        "msgid": msgid,
        "chatid": str(body.get("chatid") or "").strip(),
        "chattype": str(body.get("chattype") or "").strip(),
        "from_userid": str((body.get("from") or {}).get("userid") or "").strip(),
        "msgtype": msgtype or "unknown",
        "text_content": text.strip(),
        "quote": body.get("quote") or {},
        "response_url": str(body.get("response_url") or "").strip(),
    }


def find_binding_matches(text: str, index: dict[str, Any]) -> list[str]:
    """在文本里找出现的审批单编号（用真实编号集合做子串校验，命中的才算）。"""
    text = str(text or "")
    return [code for code in index if code and code in text]


def parse_node_command(text: str) -> tuple[bool, str, str]:
    """识别 `#节点 [类型] 摘要`。返回 (是否节点, 类型, 摘要)。"""
    text = str(text or "")
    idx = text.find(NODE_KEYWORD)
    if idx < 0:
        return False, "", ""
    rest = text[idx + len(NODE_KEYWORD) :].strip()
    parts = rest.split(None, 1)
    if len(parts) >= 2 and len(parts[0]) <= 8:
        return True, parts[0], parts[1].strip()
    return True, "", rest


def build_requirement_index(
    sheet_client: Any, docid: str, sheet_title: str
) -> tuple[str, dict[str, dict[str, Any]]]:
    """构建 审批单编号/sp_no → 需求行 索引（用于绑定匹配）。"""
    sheet_id = _sheet_id_by_title(sheet_client.get_sheets(docid), sheet_title)
    index: dict[str, dict[str, Any]] = {}
    if not sheet_id:
        return "", index
    fields = sheet_client.get_fields(docid, sheet_id)
    no_fid = _field_id_by_title(fields, APPROVAL_NO_FIELD)
    link_fid = _field_id_by_title(fields, APPROVAL_LINK_FIELD)
    records = sheet_client.get_records(docid, sheet_id).get("records") or []
    for record in records:
        if not isinstance(record, dict):
            continue
        rid = _record_id(record)
        if not rid:
            continue
        values = record_values(record)
        code = first_text_cell(values.get(no_fid) or values.get(APPROVAL_NO_FIELD)).strip()
        if code:
            index[code] = {"record_id": rid, "requirement_key": code}
        for link in _links_from_cell(values.get(link_fid) or values.get(APPROVAL_LINK_FIELD)):
            sp = parse_sp_no_from_link(link)
            if sp:
                index.setdefault(sp, {"record_id": rid, "requirement_key": sp})
    return sheet_id, index


# ---------------- 帧处理（注入 store/client/index，可单测） ----------------

def _safe_reply(client: Any, response_url: str, text: str) -> None:
    if not response_url:
        return
    try:
        client.reply(response_url, text)
    except Exception as exc:  # noqa: BLE001
        print(f"[群监听] 回复失败：{exc}")


def handle_frame(
    frame: dict[str, Any],
    *,
    store: Any,
    client: Any,
    index: dict[str, dict[str, Any]],
    profile: str,
    docid: str,
    sheet_title: str,
) -> str:
    """处理一帧。返回动作标签（bound/guide/ambiguous/stored/ignored）便于测试。"""
    msg = parse_callback(frame)
    if not msg or not msg["chatid"]:
        return "ignored"
    chatid = msg["chatid"]
    text = msg["text_content"]
    resp = msg["response_url"]
    binding = store.get_group_binding(chatid)
    explicit = BIND_KEYWORD in text

    if binding is None or explicit:
        matches = find_binding_matches(text, index)
        if len(matches) == 1:
            code = matches[0]
            rec = index[code]
            store.upsert_group_binding(
                provider="wecom",
                env_profile=profile,
                chatid=chatid,
                external_doc_id=docid,
                sheet_title=sheet_title,
                record_id=rec["record_id"],
                requirement_key=rec["requirement_key"],
                bound_by=msg["from_userid"],
            )
            filled = store.assign_chat_messages_to_record(chatid, rec["record_id"])
            _safe_reply(client, resp, f"✅ 已关联审批单编号 {code}（回填 {filled} 条历史消息）")
            return "bound"
        if len(matches) > 1:
            _safe_reply(client, resp, "识别到多个审批单编号：" + "、".join(matches[:5]) + "，请只保留一个再发我")
            return "ambiguous"
        if explicit or binding is None:
            _safe_reply(client, resp, "本群尚未关联需求，请把群名复制发我（群名含审批单编号）完成关联")
            # 继续把这条消息也存下来（绑定后回填 record_id）

    record_id = binding["record_id"] if binding else ""
    inserted = store.insert_group_message(
        msgid=msg["msgid"],
        chatid=chatid,
        from_userid=msg["from_userid"],
        msgtype=msg["msgtype"],
        text_content=text,
        quote_json=msg["quote"],
        media_paths=[],
        record_id=record_id,
        ts=None,
        raw_json=frame,
    )
    if inserted:
        is_node, cat, summary = parse_node_command(text)
        if is_node:
            store.mark_message_node(msg["msgid"], cat, summary)
            _safe_reply(client, resp, "✅ 已标记为研发节点")
            return "node"
    return "stored" if inserted else "dup"


# ---------------- 常驻循环（含断线重连） ----------------

def _default_groupbot_factory(profile: str) -> WeComGroupBotClient:
    bot_id = get_profiled_env("GROUPBOT_ID", "WECOM", profile)
    secret = get_profiled_env("GROUPBOT_SECRET", "WECOM", profile)
    if not bot_id or not secret:
        raise RuntimeError(f"{profile} 缺少 WECOM_{profile}_GROUPBOT_ID 或 GROUPBOT_SECRET，跳过群监听。")
    return WeComGroupBotClient(bot_id, secret)


def _default_smartsheet_factory(profile: str) -> WeComSmartsheetClient:
    credentials = credentials_for_profile(profile)
    if not credentials:
        raise RuntimeError(f"{profile} 缺少 WECOM_{profile}_CORP_ID / APP_SECRET。")
    cred = credentials[0]
    return WeComSmartsheetClient(cred.corpid, cred.secret)


def run_group_listener(
    profiles_arg: str = "",
    *,
    store: Any | None = None,
    groupbot_factory: Callable[[str], Any] = _default_groupbot_factory,
    smartsheet_factory: Callable[[str], Any] = _default_smartsheet_factory,
    docid: str = DEFAULT_DOCID,
    sheet_title: str = DEFAULT_SHEET_TITLE,
    index_ttl: int = 600,
    ping_interval: int = 25,
    max_seconds: float | None = None,
    max_frames: int | None = None,
) -> int:
    """常驻：连长连接收群@消息→绑定/入库；断线指数退避重连。返回处理帧数。"""
    profiles = env_profiles(profiles_arg)
    profile = profiles[0] if profiles else ""
    if not profile:
        print("[群监听] 未配置 WECOM_ENV_PROFILES / --profiles。")
        return 0

    owned_store = store is None
    store = store or open_store()
    deadline = (time.time() + max_seconds) if max_seconds else None
    handled = 0
    backoff = 1.0
    index: dict[str, dict[str, Any]] = {}
    index_at = 0.0
    try:
        while deadline is None or time.time() < deadline:
            client = None
            try:
                client = groupbot_factory(profile)
                client.connect()
                print(f"[群监听] {profile} 已连接长连接。")
                backoff = 1.0
                last_ping = time.time()
                while deadline is None or time.time() < deadline:
                    if time.time() - index_at > index_ttl:
                        try:
                            _, index = build_requirement_index(smartsheet_factory(profile), docid, sheet_title)
                            index_at = time.time()
                            print(f"[群监听] 需求索引刷新：{len(index)} 个审批单编号。")
                        except Exception as exc:  # noqa: BLE001
                            print(f"[群监听] 需求索引刷新失败：{exc}")
                            index_at = time.time()  # 失败也别每帧重试
                    if time.time() - last_ping > ping_interval:
                        client.ping()
                        last_ping = time.time()
                    frame = client.recv()
                    if frame is None:
                        continue
                    try:
                        handle_frame(
                            frame, store=store, client=client, index=index,
                            profile=profile, docid=docid, sheet_title=sheet_title,
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"[群监听] 处理消息异常（已跳过）：{exc}")
                    handled += 1
                    if max_frames is not None and handled >= max_frames:
                        return handled
            except Exception as exc:  # noqa: BLE001
                wait = min(backoff, 30.0)
                print(f"[群监听] 连接异常，{wait:.0f}s 后重连：{exc}")
                time.sleep(wait)
                backoff = min(backoff * 2, 30.0)
            finally:
                if client is not None:
                    client.close()
    finally:
        if owned_store:
            close_store(store)
    return handled
