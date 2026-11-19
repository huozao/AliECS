"""邮件投递通道（SMTP / STARTTLS / SSL）。

支持两类载荷：
1. 原生载荷（channel_payloads["email"]）：允许业务方直接提供定制 HTML（例如畅捷通
   家里库存存量表、SSO 验证码邮件）。
2. 通用降级渲染：将 Notification 的通用字段（标题、摘要、段落、字段集、按钮等）
   自动渲染为结构优雅、跨邮件客户端兼容的自适应 HTML 邮件。

凭据默认自环境读取（SMTP_HOST/PORT/USER/PASS/FROM），并兼容既有 EMAIL_ADDRESS_QQ /
EMAIL_PASSWORD_QQ。任何连接或发送失败均抛出异常，由 dispatch 记账并安排重试。
"""

from __future__ import annotations

import html
import json
import logging
import os
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any

from app.notify.models import LEVEL_ICONS, Notification, NotifySegment

logger = logging.getLogger(__name__)

DEFAULT_SMTP_HOST = "smtp.qq.com"
DEFAULT_SMTP_PORT = 587
DEFAULT_TIMEOUT_SECONDS = 20.0

THEME_COLORS = {
    "info": {"primary": "#1677ff", "bg": "#f0f5ff", "border": "#adc6ff"},
    "warn": {"primary": "#fa8c16", "bg": "#fff7e6", "border": "#ffd591"},
    "error": {"primary": "#f5222d", "bg": "#fff1f0", "border": "#ffa39e"},
    "fatal": {"primary": "#820014", "bg": "#fff1f0", "border": "#cf1322"},
}


def credentials(profile: str = "default") -> dict[str, Any]:
    """读取指定 profile 的 SMTP 凭据配置。"""
    prefix = f"SMTP_{profile.upper()}_" if profile and profile != "default" else "SMTP_"

    host = os.getenv(f"{prefix}HOST") or os.getenv("SMTP_HOST") or DEFAULT_SMTP_HOST
    port_str = os.getenv(f"{prefix}PORT") or os.getenv("SMTP_PORT") or str(DEFAULT_SMTP_PORT)
    try:
        port = int(port_str)
    except ValueError:
        port = DEFAULT_SMTP_PORT

    secure_str = os.getenv(f"{prefix}SECURE") or os.getenv("SMTP_SECURE", "")
    secure = secure_str.lower() in ("true", "1", "yes") or port == 465

    user = (
        os.getenv(f"{prefix}USER")
        or os.getenv("SMTP_USER")
        or os.getenv("EMAIL_ADDRESS_QQ")
        or ""
    ).strip()

    password = (
        os.getenv(f"{prefix}PASS")
        or os.getenv("SMTP_PASS")
        or os.getenv("EMAIL_PASSWORD_QQ")
        or os.getenv("EMAIL_PASSWOR_QQ")
        or ""
    ).strip()

    from_addr = (
        os.getenv(f"{prefix}FROM")
        or os.getenv("SMTP_FROM")
        or user
    ).strip()

    return {
        "host": host,
        "port": port,
        "secure": secure,
        "user": user,
        "password": password,
        "from_addr": from_addr,
        "timeout": DEFAULT_TIMEOUT_SECONDS,
    }


def _normalize_recipients(val: Any) -> list[str]:
    if not val:
        return []
    if isinstance(val, str):
        parts = [p.strip() for p in val.replace(";", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(val, (list, tuple, set)):
        result = []
        for item in val:
            if isinstance(item, str):
                parts = [p.strip() for p in item.replace(";", ",").split(",")]
                result.extend([p for p in parts if p])
        return result
    return []


def render_html(notification: Notification) -> str:
    """将通用 Notification 渲染为响应式 HTML 邮件正文。"""
    color = THEME_COLORS.get(notification.level, THEME_COLORS["info"])
    icon = LEVEL_ICONS.get(notification.level, "ℹ️")

    segments_html = []
    for seg in notification.segments:
        if seg.kind == "text":
            escaped_text = html.escape(seg.text)
            if seg.preformatted:
                segments_html.append(
                    f'<pre style="background:#f5f5f5;padding:12px;border-radius:4px;overflow-x:auto;'
                    f'font-family:Consolas,monospace;font-size:13px;line-height:1.5;">{escaped_text}</pre>'
                )
            else:
                lines = [f"<p style='margin:6px 0;line-height:1.6;'>{html.escape(line)}</p>" for line in seg.text.split("\n") if line.strip()]
                segments_html.append("".join(lines))

        elif seg.kind == "fields":
            rows = []
            for f in seg.fields:
                note_html = f"<div style='font-size:12px;color:#888;margin-top:2px;'>{html.escape(f.note)}</div>" if f.note else ""
                rows.append(
                    f"<tr><td style='padding:6px 10px;background:#fafafa;border:1px solid #e8e8e8;width:30%;"
                    f"font-weight:600;color:#555;'>{html.escape(f.name)}</td>"
                    f"<td style='padding:6px 10px;border:1px solid #e8e8e8;color:#222;'>"
                    f"{html.escape(f.value)}{note_html}</td></tr>"
                )
            table = f"<table style='width:100%;border-collapse:collapse;margin:10px 0;font-size:13px;'>{''.join(rows)}</table>"
            segments_html.append(table)

        elif seg.kind == "section":
            sec_icon = seg.section_icon or "📌"
            rows = []
            for f in seg.fields:
                note_html = f"<div style='font-size:12px;color:#888;margin-top:2px;'>{html.escape(f.note)}</div>" if f.note else ""
                rows.append(
                    f"<tr><td style='padding:6px 10px;background:#fafafa;border:1px solid #e8e8e8;width:30%;"
                    f"font-weight:600;color:#555;'>{html.escape(f.name)}</td>"
                    f"<td style='padding:6px 10px;border:1px solid #e8e8e8;color:#222;'>"
                    f"{html.escape(f.value)}{note_html}</td></tr>"
                )
            table = f"<table style='width:100%;border-collapse:collapse;margin:8px 0;font-size:13px;'>{''.join(rows)}</table>"
            sub_title_html = f"<div style='font-size:12px;color:#666;margin-top:2px;'>{html.escape(seg.section_subtitle)}</div>" if seg.section_subtitle else ""
            section_box = (
                f"<div style='margin:12px 0;border:1px solid #e8e8e8;border-radius:6px;overflow:hidden;'>"
                f"<div style='background:#f0f2f5;padding:8px 12px;font-weight:600;font-size:14px;color:#333;'>"
                f"{sec_icon} {html.escape(seg.section_title)}{sub_title_html}</div>"
                f"<div style='padding:8px 12px;'>{table}</div></div>"
            )
            segments_html.append(section_box)

    buttons_html = []
    if notification.buttons:
        btns = []
        for btn in notification.buttons:
            btns.append(
                f"<a href='{html.escape(btn.url)}' target='_blank' style='display:inline-block;padding:8px 16px;"
                f"margin-right:8px;margin-bottom:8px;background:{color['primary']};color:#fff;text-decoration:none;"
                f"border-radius:4px;font-size:13px;font-weight:500;'>{html.escape(btn.text)}</a>"
            )
        buttons_html.append(f"<div style='margin-top:16px;'>{''.join(btns)}</div>")

    link_html = ""
    if notification.link:
        link_html = (
            f"<div style='margin-top:12px;font-size:13px;'>🔗 链接：<a href='{html.escape(notification.link.url)}' "
            f"target='_blank' style='color:{color['primary']};'>{html.escape(notification.link.text)}</a></div>"
        )

    summary_html = ""
    if notification.summary:
        summary_html = (
            f"<div style='background:{color['bg']};border-left:4px solid {color['primary']};padding:10px 14px;"
            f"margin-bottom:16px;border-radius:0 4px 4px 0;font-size:14px;line-height:1.5;color:#333;'>"
            f"{html.escape(notification.summary)}</div>"
        )

    time_str = notification.occurred_at.strftime("%Y-%m-%d %H:%M:%S") if notification.occurred_at else ""
    footer_text = f"来源: {html.escape(notification.source)} · 事件: {html.escape(notification.event)}"
    if time_str:
        footer_text += f" · 时间: {time_str}"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:20px;background:#f5f7fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
  <div style="max-width:680px;margin:0 auto;background:#fff;border-radius:8px;border:1px solid #e8e8e8;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.05);">
    <div style="background:{color['primary']};color:#fff;padding:16px 20px;">
      <h2 style="margin:0;font-size:18px;font-weight:600;line-height:1.4;">{icon} {html.escape(notification.title)}</h2>
      {f"<div style='margin-top:4px;font-size:13px;opacity:0.9;'>{html.escape(notification.subtitle)}</div>" if notification.subtitle else ""}
    </div>
    <div style="padding:20px;">
      {summary_html}
      {''.join(segments_html)}
      {link_html}
      {''.join(buttons_html)}
    </div>
    <div style="background:#fafafa;border-top:1px solid #f0f0f0;padding:10px 20px;font-size:12px;color:#888;">
      {footer_text}
    </div>
  </div>
</body>
</html>"""


def _extract_email_content(notification: Notification) -> tuple[str, str]:
    """提取邮件正文。返回 (html_body, text_summary)。"""
    payload = notification.channel_payloads.get("email")
    if payload is not None:
        content = payload.content
        if isinstance(content, dict):
            html_text = str(content.get("html") or "").strip()
            plain_text = str(content.get("text") or "").strip()
            if html_text:
                return html_text, plain_text or notification.summary or notification.title
            if plain_text:
                return f"<pre style='font-family:sans-serif;'>{html.escape(plain_text)}</pre>", plain_text
        elif isinstance(content, str):
            text_str = content.strip()
            if "<html" in text_str.lower() or "<table" in text_str.lower() or "<div" in text_str.lower():
                return text_str, notification.summary or notification.title
            return f"<pre style='font-family:sans-serif;'>{html.escape(text_str)}</pre>", text_str

    rendered_html = render_html(notification)
    plain_summary = notification.summary or notification.title
    return rendered_html, plain_summary


def send(
    notification: Notification,
    target: dict[str, Any],
    *,
    smtp_client_factory: Any = None,
) -> None:
    """投递邮件通知。失败抛出异常，由 dispatch 记账重试。

    target 结构示例:
    {
        "to": ["user@example.com", "admin@example.com"],
        "cc": ["manager@example.com"],
        "subject": "可选覆盖标题",
        "profile": "default"
    }
    """
    to_list = _normalize_recipients(target.get("to") or target.get("recipients"))
    if not to_list:
        raise RuntimeError("email target has no recipients in 'to' or 'recipients'")

    cc_list = _normalize_recipients(target.get("cc"))
    profile = str(target.get("profile") or "default").strip()
    creds = credentials(profile)

    if not creds["user"] or not creds["password"]:
        raise RuntimeError(
            f"SMTP credentials missing for profile '{profile}'; "
            f"check SMTP_USER/SMTP_PASS or EMAIL_ADDRESS_QQ/EMAIL_PASSWORD_QQ"
        )

    subject = str(target.get("subject") or "").strip() or notification.title

    html_body, plain_text = _extract_email_content(notification)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = creds["from_addr"]
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Date"] = formatdate(localtime=True)

    part_text = MIMEText(plain_text, "plain", "utf-8")
    part_html = MIMEText(html_body, "html", "utf-8")
    msg.attach(part_text)
    msg.attach(part_html)

    all_recipients = list(set(to_list + cc_list))

    if smtp_client_factory is not None:
        client = smtp_client_factory(creds["host"], creds["port"], timeout=creds["timeout"])
        client.login(creds["user"], creds["password"])
        client.send_message(msg, from_addr=creds["from_addr"], to_addrs=all_recipients)
        return

    # 生产网络发信逻辑
    if creds["secure"]:
        server = smtplib.SMTP_SSL(creds["host"], creds["port"], timeout=creds["timeout"])
    else:
        server = smtplib.SMTP(creds["host"], creds["port"], timeout=creds["timeout"])

    try:
        if not creds["secure"]:
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls()
                server.ehlo()
        server.login(creds["user"], creds["password"])
        server.send_message(msg, from_addr=creds["from_addr"], to_addrs=all_recipients)
        logger.info(
            "email sent successfully subject=%s to=%s cc=%s",
            subject, to_list, cc_list,
        )
    finally:
        try:
            server.quit()
        except Exception:
            pass
