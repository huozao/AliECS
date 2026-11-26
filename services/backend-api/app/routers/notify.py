"""统一消息中枢的 HTTP 入口。

只有两个面向机器的端点：外部生产者投消息（/send），以及带走积压重试（/flush）。
服务端内部的生产者不走这里——它们直接 import app.notify.dispatch.deliver。
"""

from __future__ import annotations

import html
import hmac
import os
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from app.core import _conn, _database_url
from app.notify.channels import feishu
from app.notify import decision_realtime, dispatch, personal_wechat, store
from app.notify.models import Notification


@asynccontextmanager
async def _notify_lifespan(_app: Any):
    try:
        yield
    finally:
        await decision_realtime.shutdown_decision_streams()


router = APIRouter(lifespan=_notify_lifespan)


def _require_source(
    x_notify_source: str | None = Header(default=None),
    x_notify_token: str | None = Header(default=None),
) -> str:
    """按来源校验 token。每个来源一个 token，可单独吊销。"""
    source_key = (x_notify_source or "").strip()
    token = (x_notify_token or "").strip()
    if not (source_key and token):
        raise HTTPException(status_code=401, detail="missing notify credentials")
    with closing(_conn()) as conn:
        if not store.verify_source_token(conn, source_key, token):
            raise HTTPException(status_code=401, detail="invalid notify credentials")
    return source_key


def _require_flush_token(x_notify_flush_token: str | None = Header(default=None)) -> None:
    """flush 是内部端点（worker 主循环每轮调一次），单独一个 token。"""
    expected = os.getenv("NOTIFY_FLUSH_TOKEN", "").strip()
    supplied = (x_notify_flush_token or "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="invalid flush token")


@router.post("/v1/internal/notify/send")
def send_notification(
    body: Notification,
    source_key: str = Depends(_require_source),
) -> dict[str, Any]:
    """投递一条通知。

    ``source`` 必须与请求头里的来源一致——否则一个来源的 token 就能冒充别的来源，
    绕开按来源配置的路由。
    """
    if body.source != source_key:
        raise HTTPException(status_code=403, detail="source mismatch")
    result = dispatch.deliver(body)
    if result["duplicate"]:
        if result["sent"] > 0:
            return {"ok": True, "delivered": True, **result}
        reason = (
            "no matching route"
            if result["targets"] == 0
            else "queued for retry"
            if result.get("pending", 0) > 0
            else "delivery exhausted"
        )
        return {"ok": True, "delivered": False, "reason": reason, **result}
    if result["targets"] == 0:
        # 落库了但没有任何路由命中。显式返回 delivered=false：调用方需要知道
        # 「收下了」和「发出去了」不是一回事，否则配置缺失会被当成投递成功。
        return {"ok": True, "delivered": False, "reason": "no matching route", **result}
    if result["sent"] == 0:
        raise HTTPException(
            status_code=502,
            detail=f"all {result['targets']} targets failed; queued for retry (outbox {result['outbox_id']})",
        )
    return {"ok": True, "delivered": True, **result}



def _wechat_agent(
    x_notify_wechat_agent: str = Header(default=""),
    x_notify_wechat_token: str = Header(default=""),
) -> str:
    return personal_wechat.require_agent(x_notify_wechat_agent, x_notify_wechat_token)


@router.post("/v1/internal/notify/send-wechat", status_code=202)
async def send_wechat(request: Request, source: str = Depends(_require_source)):
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > 4_100_000:
            raise HTTPException(413, "wechat notification too large")
    try:
        body = personal_wechat.Submission.model_validate_json(buffer)
    except ValueError:
        raise HTTPException(422, "invalid wechat notification") from None
    def save():
        with closing(_conn()) as conn:
            result = personal_wechat.submit(conn, source, body)
            conn.commit()
        return result

    return await run_in_threadpool(save)


@router.post("/v1/internal/notify/wechat/claim")
def claim_wechat(agent: str = Depends(_wechat_agent)):
    with closing(_conn()) as conn:
        task = personal_wechat.claim(conn, agent)
        conn.commit()
    return {"task": task}


@router.post("/v1/internal/notify/wechat/{delivery_id}/begin")
def begin_wechat(delivery_id: int, body: personal_wechat.Lease, agent: str = Depends(_wechat_agent)):
    with closing(_conn()) as conn:
        result = personal_wechat.begin(conn, agent, delivery_id, body.lease)
        conn.commit()
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store"})


@router.post("/v1/internal/notify/wechat/{delivery_id}/receipt")
def finish_wechat(delivery_id: int, body: personal_wechat.Receipt, agent: str = Depends(_wechat_agent)):
    with closing(_conn()) as conn:
        result = personal_wechat.finish(conn, agent, delivery_id, body)
        conn.commit()
    return result


@router.get("/v1/internal/notify/deliveries/{outbox_id}")
def get_delivery_receipt(
    outbox_id: int,
    source_key: str = Depends(_require_source),
) -> dict[str, Any]:
    """返回一条消息的实际投递凭证，并按已鉴权来源隔离。"""
    with closing(_conn()) as conn:
        receipt = store.delivery_receipt(conn, outbox_id, source_key)
    if receipt is None:
        # 不区分“不存在”和“属于其他来源”，避免泄漏其他生产者的消息编号。
        raise HTTPException(status_code=404, detail="notification delivery not found")
    return {"ok": True, **receipt}


@router.post("/v1/internal/notify/flush")
def flush_pending(
    limit: int = 50,
    _: None = Depends(_require_flush_token),
) -> dict[str, Any]:
    if os.getenv("NOTIFY_WECHAT_PAYLOAD_KEY"):
        with closing(_conn()) as conn:
            personal_wechat.cleanup(conn)
            conn.commit()
    return {"ok": True, **dispatch.flush(limit=max(1, min(limit, 200)))}


_BEIJING_TZ = timezone(timedelta(hours=8))


def _format_beijing_time(dt: Any) -> str:
    if dt is None:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            return dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return str(dt)


def _render_decision_page(
    decision: dict[str, Any], token: str, selected_opt: str = ""
) -> str:
    question_esc = html.escape(str(decision.get("question") or ""), quote=True)
    status = str(decision.get("status") or "pending")
    chosen_label = decision.get("chosen_label")
    chosen_at = decision.get("chosen_at")

    if status == "answered" and chosen_label:
        bj_time = _format_beijing_time(chosen_at)
        status_html = (
            f"<div class='status answered'>当前状态：<strong>已选 {html.escape(chosen_label, quote=True)}</strong>"
            f"<span class='time'>（北京时间 {html.escape(bj_time, quote=True)}）</span></div>"
        )
    else:
        status_html = "<div class='status pending'>当前状态：<span>未选</span></div>"

    options = decision.get("options") or []
    target_opt = next((o for o in options if o.get("key") == selected_opt), None)

    options_html_parts: list[str] = []
    for opt in options:
        opt_key = opt.get("key", "")
        opt_label = opt.get("label", "")
        is_target = target_opt is not None and opt_key == target_opt.get("key")
        is_current_chosen = status == "answered" and opt_key == decision.get("chosen_option")
        badge = ""
        if is_current_chosen:
            badge = " <span class='badge chosen'>已生效</span>"
        elif is_target:
            badge = " <span class='badge target'>待确认</span>"
        cls = "opt-item"
        if is_target:
            cls += " highlighted"
        options_html_parts.append(
            f"<li class='{cls}'><span class='opt-label'>{html.escape(opt_label, quote=True)}</span>"
            f"<span class='opt-key'>({html.escape(opt_key, quote=True)})</span>{badge}</li>"
        )
    options_list_html = "".join(options_html_parts)

    token_esc = html.escape(token, quote=True)
    if target_opt is not None:
        action_button_html = (
            f"<form method='POST' action=''>"
            f"<input type='hidden' name='t' value='{token_esc}'>"
            f"<input type='hidden' name='opt' value='{html.escape(target_opt.get('key', ''), quote=True)}'>"
            f"<button type='submit' class='btn btn-primary'>确认选择：{html.escape(target_opt.get('label', ''), quote=True)}</button>"
            f"</form>"
        )
    else:
        # 未指定具体 opt 时，允许点击任意一项进行提交
        buttons: list[str] = []
        for opt in options:
            buttons.append(
                f"<form method='POST' action='' style='margin-bottom:8px;'>"
                f"<input type='hidden' name='t' value='{token_esc}'>"
                f"<input type='hidden' name='opt' value='{html.escape(opt.get('key', ''), quote=True)}'>"
                f"<button type='submit' class='btn btn-default'>确认选择：{html.escape(opt.get('label', ''), quote=True)}</button>"
                f"</form>"
            )
        action_button_html = "".join(buttons)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>决策确认</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #f5f6f7; color: #1f2329; padding: 20px 16px; line-height: 1.5; }}
.card {{ max-width: 480px; margin: 24px auto; background: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); padding: 24px; }}
.title {{ font-size: 14px; font-weight: 600; color: #646a73; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
.question {{ font-size: 18px; font-weight: 700; color: #1f2329; margin-bottom: 16px; word-break: break-word; }}
.status {{ font-size: 14px; padding: 10px 12px; border-radius: 8px; margin-bottom: 20px; }}
.status.pending {{ background: #eff0f1; color: #646a73; }}
.status.answered {{ background: #e8f3ff; color: #1664ff; }}
.status .time {{ font-size: 12px; color: #8f959e; margin-left: 4px; }}
.section-title {{ font-size: 13px; font-weight: 600; color: #8f959e; margin-bottom: 8px; }}
.options-list {{ list-style: none; margin-bottom: 24px; }}
.opt-item {{ padding: 10px 12px; border-radius: 6px; border: 1px solid #dee0e3; margin-bottom: 6px; display: flex; align-items: center; }}
.opt-item.highlighted {{ border-color: #3370ff; background: #f0f4ff; }}
.opt-label {{ font-weight: 600; font-size: 15px; color: #1f2329; }}
.opt-key {{ font-size: 12px; color: #8f959e; margin-left: 6px; }}
.badge {{ font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: auto; font-weight: 500; }}
.badge.chosen {{ background: #e8ffea; color: #009a29; }}
.badge.target {{ background: #e8f3ff; color: #1664ff; }}
.btn {{ width: 100%; display: block; padding: 12px 16px; font-size: 16px; font-weight: 600; border-radius: 8px; border: none; cursor: pointer; text-align: center; }}
.btn-primary {{ background: #3370ff; color: #ffffff; }}
.btn-primary:active {{ background: #285cd9; }}
.btn-default {{ background: #eff0f1; color: #1f2329; }}
.btn-default:active {{ background: #dee0e3; }}
.hint {{ font-size: 12px; color: #8f959e; text-align: center; margin-top: 14px; }}
</style>
</head>
<body>
<div class="card">
    <div class="title">决策确认</div>
    <div class="question">{question_esc}</div>
    {status_html}
    <div class="section-title">选项列表</div>
    <ul class="options-list">
        {options_list_html}
    </ul>
    {action_button_html}
    <div class="hint">点击确认后将记录您的选择，支持后续改选（以最后一次为准）</div>
</div>
</body>
</html>"""


def _render_decision_result_page(
    decision: dict[str, Any], option_label: str, chosen_at: datetime
) -> str:
    question_esc = html.escape(str(decision.get("question") or ""), quote=True)
    label_esc = html.escape(option_label, quote=True)
    bj_time_esc = html.escape(_format_beijing_time(chosen_at), quote=True)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>选择已记录</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #f5f6f7; color: #1f2329; padding: 20px 16px; line-height: 1.5; }}
.card {{ max-width: 480px; margin: 24px auto; background: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); padding: 24px; text-align: center; }}
.icon {{ font-size: 48px; margin-bottom: 12px; }}
.result-title {{ font-size: 20px; font-weight: 700; color: #009a29; margin-bottom: 8px; }}
.result-detail {{ font-size: 15px; color: #1f2329; margin-bottom: 16px; word-break: break-word; }}
.meta {{ font-size: 13px; color: #8f959e; margin-bottom: 20px; }}
.question-box {{ background: #f7f8fa; border-radius: 8px; padding: 12px; font-size: 14px; color: #646a73; margin-bottom: 20px; text-align: left; }}
.hint {{ font-size: 12px; color: #8f959e; }}
</style>
</head>
<body>
<div class="card">
    <div class="icon">✅</div>
    <div class="result-title">已记录：{label_esc}</div>
    <div class="meta">（北京时间 {bj_time_esc}）</div>
    <div class="question-box"><strong>问题：</strong>{question_esc}</div>
    <div class="hint">记录已生效。历史改选记录已全部保留，随时可通过原链接改选。</div>
</div>
</body>
</html>"""


@router.get("/v1/notify/decisions/{decision_id}", response_class=HTMLResponse)
def get_decision_page(
    decision_id: int,
    t: str = "",
    opt: str = "",
) -> HTMLResponse:
    """公网决策确认页。GET 绝对只读，不修改任何数据库状态。"""
    token = t.strip()
    if not token:
        raise HTTPException(status_code=404, detail="decision not found")
    with closing(_conn()) as conn:
        decision = store.get_decision_by_id(conn, decision_id)
        if decision is None:
            raise HTTPException(status_code=404, detail="decision not found")
        if not store.verify_decision_token(conn, decision_id, token):
            raise HTTPException(status_code=404, detail="decision not found")

    content = _render_decision_page(decision, token=token, selected_opt=opt.strip())
    return HTMLResponse(content=content, status_code=200)


@router.post("/v1/notify/decisions/{decision_id}", response_class=HTMLResponse)
async def post_decision_choice(
    decision_id: int,
    request: Request,
) -> HTMLResponse:
    """公网决策选择提交。校验通过后追加选择事件并更新最新选择。"""
    token = ""
    opt = ""
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        token = str(form.get("t") or "")
        opt = str(form.get("opt") or "")
    elif "application/json" in content_type:
        try:
            body_json = await request.json()
            if isinstance(body_json, dict):
                token = str(body_json.get("t") or "")
                opt = str(body_json.get("opt") or "")
        except Exception:
            pass
    if not token:
        token = request.query_params.get("t", "")
    if not opt:
        opt = request.query_params.get("opt", "")

    token = token.strip()
    opt = opt.strip()
    if not token:
        raise HTTPException(status_code=404, detail="decision not found")

    with closing(_conn()) as conn:
        decision = store.get_decision_by_id(conn, decision_id)
        if decision is None:
            raise HTTPException(status_code=404, detail="decision not found")
        if not store.verify_decision_token(conn, decision_id, token):
            raise HTTPException(status_code=404, detail="decision not found")

        options = decision.get("options") or []
        target_opt = next((o for o in options if o.get("key") == opt), None)
        if target_opt is None:
            raise HTTPException(status_code=422, detail="invalid option")

        if (
            feishu.is_wechat_recovery_native_cards_enabled()
            and decision_realtime.should_emit_decision_wake(decision["source_key"], decision["key"], opt)
        ):
            prefix = decision["key"].split(":", 1)[0]
            current = store.get_current_wechat_recovery_decision(conn, prefix=prefix)
            if not current or current["id"] != decision_id:
                raise HTTPException(status_code=409, detail="recovery card is no longer current")
            if current["status"] == "answered":
                return HTMLResponse(_render_decision_result_page(
                    current, option_label=current["chosen_label"], chosen_at=current["chosen_at"],
                ))
            decision = current
        client_ip = request.client.host if request.client else ""
        user_agent = request.headers.get("user-agent", "")
        chosen_at = store.record_decision_choice_with_wake(
            conn,
            decision=decision,
            option_key=target_opt["key"],
            option_label=target_opt["label"],
            client_ip=client_ip,
            user_agent=user_agent,
        )
        conn.commit()

    content = _render_decision_result_page(decision, option_label=target_opt["label"], chosen_at=chosen_at)
    return HTMLResponse(content=content, status_code=200)


def _require_feishu_recovery_token(
    x_notify_feishu_action_token: str | None = Header(default=None),
) -> None:
    """原生飞书恢复动作独立服务间令牌校验（fail-closed）。"""
    expected = decision_realtime.feishu_recovery_gateway_token()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="feishu recovery gateway token is not configured",
        )
    supplied = (x_notify_feishu_action_token or "").strip()
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="invalid feishu action token")


class FeishuDecisionChoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    decision_key: str = Field(min_length=1, max_length=128)
    option_key: str = Field(min_length=1, max_length=32)
    operator_open_id: str = Field(min_length=1, max_length=128)
    chat_id: str = Field(min_length=1, max_length=128)

    @field_validator("decision_key", "option_key", "operator_open_id", "chat_id")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("field must not be empty")
        return stripped


@router.get("/v1/internal/notify/decisions/stream")
async def stream_decision_wakes(
    source_key: str = Depends(_require_source),
) -> StreamingResponse:
    """来源授权的实时决策唤醒 SSE（基于 PostgreSQL LISTEN/NOTIFY）。"""
    db_url = _database_url()
    try:
        initial_conn = await decision_realtime.start_decision_listen(db_url)
    except Exception as exc:
        decision_realtime.logger.warning(
            "decision stream listen init failed source=%s error=%s",
            source_key,
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="decision stream unavailable") from None

    handle: decision_realtime.StreamHandle | None = None
    try:
        handle = decision_realtime.register_stream(initial_conn)
        heartbeat_sec = decision_realtime.heartbeat_seconds()
        reconnect_init, reconnect_max = decision_realtime.reconnect_bounds()
        stream = decision_realtime.decision_wake_stream(
            source_key=source_key,
            database_url=db_url,
            initial_conn=initial_conn,
            handle=handle,
            heartbeat_interval=heartbeat_sec,
            reconnect_initial=reconnect_init,
            reconnect_max=reconnect_max,
        )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception:
        if handle is not None:
            decision_realtime._active_streams.discard(handle)
        await decision_realtime.close_listen_connection(initial_conn)
        raise


@router.post("/v1/internal/notify/decisions/feishu-choice")
def post_feishu_decision_choice(
    body: FeishuDecisionChoiceRequest,
    request: Request,
    _: None = Depends(_require_feishu_recovery_token),
) -> dict[str, Any]:
    """原生飞书卡片恢复动作入口：仅限 wechat-digest 当前 QR 决策，fail-closed 授权。"""
    allowed_chats = decision_realtime.feishu_recovery_allowed_chats()
    if not allowed_chats:
        raise HTTPException(
            status_code=503,
            detail="feishu recovery chat allowlist is not configured",
        )
    allowed_operators = decision_realtime.feishu_recovery_allowed_operators()
    if not allowed_operators:
        raise HTTPException(
            status_code=503,
            detail="feishu recovery operator allowlist is not configured",
        )
    if not decision_realtime.matches_allowlist(body.chat_id, allowed_chats):
        raise HTTPException(status_code=403, detail="chat_id not allowed")
    if not decision_realtime.matches_allowlist(body.operator_open_id, allowed_operators):
        raise HTTPException(status_code=403, detail="operator_open_id not allowed")
    if not decision_realtime.is_wechat_recovery_decision_key(body.decision_key):
        raise HTTPException(
            status_code=403,
            detail="only wechat-digest QR recovery decisions are allowed",
        )

    client_ip = request.client.host if request.client else ""
    with closing(_conn()) as conn:
        current = store.get_current_wechat_recovery_decision(
            conn, for_update=True, **({"prefix": "telegram-login"} if body.decision_key.startswith("telegram-login:") else {})
        )
        if current is None:
            raise HTTPException(status_code=404, detail="current recovery decision not found")
        if current["key"] != body.decision_key:
            raise HTTPException(status_code=409, detail="decision is not the current QR decision")
        options = current.get("options") or []
        target_opt = next((o for o in options if o.get("key") == body.option_key), None)
        if target_opt is None or body.option_key not in decision_realtime.WECHAT_RECOVERY_OPTIONS:
            raise HTTPException(status_code=422, detail="invalid option")
        if (
            current.get("status") == "answered"
            and current.get("chosen_option") == target_opt["key"]
        ):
            conn.commit()
            return {
                "ok": True,
                "decision_key": body.decision_key,
                "option_key": target_opt["key"],
                "newly_recorded": False,
            }
        if current.get("status") == "answered":
            raise HTTPException(status_code=409, detail="recovery choice already recorded")
        store.record_decision_choice_with_wake(
            conn,
            decision=current,
            option_key=target_opt["key"],
            option_label=target_opt["label"],
            client_ip=client_ip,
            user_agent="feishu-choice",
        )
        conn.commit()

    return {
        "ok": True,
        "decision_key": body.decision_key,
        "option_key": target_opt["key"],
        "newly_recorded": True,
    }


@router.get("/v1/internal/notify/decisions/by-key")
def get_decision_by_key_endpoint(
    key: str,
    source_key: str = Depends(_require_source),
) -> dict[str, Any]:
    """按来源和 key 查询决策详情。不存在或属于其他来源一律 404。"""
    decision_key = (key or "").strip()
    if not decision_key:
        raise HTTPException(status_code=404, detail="decision not found")
    with closing(_conn()) as conn:
        detail = store.get_decision_by_key(conn, source_key, decision_key)
    if detail is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return detail


@router.get("/v1/internal/notify/decisions")
def list_decisions_endpoint(
    status: str | None = None,
    limit: int = 50,
    source_key: str = Depends(_require_source),
) -> list[dict[str, Any]]:
    """列出当前来源的决策（按创建时间倒序）。"""
    with closing(_conn()) as conn:
        items = store.list_decisions(
            conn, source_key, status=status, limit=max(1, min(limit, 200))
        )
    return items
