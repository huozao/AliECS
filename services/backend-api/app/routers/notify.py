"""统一消息中枢的 HTTP 入口。

只有两个面向机器的端点：外部生产者投消息（/send），以及带走积压重试（/flush）。
服务端内部的生产者不走这里——它们直接 import app.notify.dispatch.deliver。
"""

from __future__ import annotations

import html
import hmac
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.core import _conn
from app.notify import dispatch, store
from app.notify.models import Notification

router = APIRouter()


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
@router.get("/api/v1/notify/decisions/{decision_id}", response_class=HTMLResponse, include_in_schema=False)
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
    if not hmac.compare_digest(decision["token_sha256"], store.token_digest(token)):
        raise HTTPException(status_code=404, detail="decision not found")

    content = _render_decision_page(decision, token=token, selected_opt=opt.strip())
    return HTMLResponse(content=content, status_code=200)


@router.post("/v1/notify/decisions/{decision_id}", response_class=HTMLResponse)
@router.post("/api/v1/notify/decisions/{decision_id}", response_class=HTMLResponse, include_in_schema=False)
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
        if not hmac.compare_digest(decision["token_sha256"], store.token_digest(token)):
            raise HTTPException(status_code=404, detail="decision not found")

        options = decision.get("options") or []
        target_opt = next((o for o in options if o.get("key") == opt), None)
        if target_opt is None:
            raise HTTPException(status_code=422, detail="invalid option")

        client_ip = request.client.host if request.client else ""
        user_agent = request.headers.get("user-agent", "")
        chosen_at = store.record_decision_choice(
            conn,
            decision_id=decision_id,
            option_key=target_opt["key"],
            option_label=target_opt["label"],
            client_ip=client_ip,
            user_agent=user_agent,
        )
        conn.commit()

    content = _render_decision_result_page(decision, option_label=target_opt["label"], chosen_at=chosen_at)
    return HTMLResponse(content=content, status_code=200)


@router.get("/v1/internal/notify/decisions/by-key")
@router.get("/api/v1/internal/notify/decisions/by-key", include_in_schema=False)
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
@router.get("/api/v1/internal/notify/decisions", include_in_schema=False)
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
