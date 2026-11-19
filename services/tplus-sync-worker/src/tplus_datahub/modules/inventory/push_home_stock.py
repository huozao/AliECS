"""畅捷通成品库家里库存邮件推送服务。

功能：
1. 从 T+ OpenAPI 现存量查询结果中筛选「成品库」记录；
2. 转换为 meidi-auto 流水线 020 步骤严格兼容的标准 HTML 表格；
3. 包含关键识别词「等待您查看」的主题构造；
4. 依托统一消息中枢（app.notify.channels.email）可靠投递。
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.notify.channels import email as email_channel
from app.notify.models import Notification, NotifyChannelPayload

logger = logging.getLogger("tplus_datahub.home_stock")
TZ_SH = ZoneInfo("Asia/Shanghai")

STOCK_TABLE_HEADERS = ["仓库", "存货编码", "存货名称", "规格型号", "主计量", "主数量"]


def filter_finished_stock(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从现存量全量行中筛选出成品库（WarehouseName == '成品库' 或 WarehouseCode == '002'）。"""
    finished_items: list[dict[str, Any]] = []

    for r in rows:
        wh_name = str(r.get("WarehouseName") or "").strip()
        wh_code = str(r.get("WarehouseCode") or "").strip()

        # 兼容仓库名称或仓库编码识别成品库
        if wh_name == "成品库" or wh_code == "002":
            inv_code = str(r.get("InventoryCode") or "").strip()
            inv_name = str(r.get("InventoryName") or "").strip()
            spec = str(r.get("Specification") or "").strip()
            unit = str(r.get("UnitName") or r.get("BaseUnitName") or "").strip() or "kg"

            qty_val = r.get("ExistingQuantity")
            if qty_val is None:
                qty_str = "0"
            else:
                try:
                    qty_num = float(qty_val)
                    qty_str = f"{qty_num:.4f}".rstrip("0").rstrip(".") if "." in f"{qty_num:.4f}" else str(int(qty_num))
                except (ValueError, TypeError):
                    qty_str = str(qty_val).strip()

            finished_items.append({
                "仓库": wh_name or "成品库",
                "存货编码": inv_code,
                "存货名称": inv_name,
                "规格型号": spec,
                "主计量": unit,
                "主数量": qty_str,
            })

    # 按存货编码排序，确保邮件呈现有序
    finished_items.sort(key=lambda x: x["存货编码"])
    return finished_items


def render_stock_html_table(items: list[dict[str, Any]], timestamp_str: str = "") -> str:
    """渲染为 meidi-auto 020 步骤能够直接通过 BeautifulSoup 解析提取的标准 HTML 表格。"""
    rows_html = []
    for item in items:
        row_tds = "".join(f"<td>{html.escape(item[h])}</td>" for h in STOCK_TABLE_HEADERS)
        rows_html.append(f"<tr>{row_tds}</tr>")

    headers_th = "".join(f"<th>{h}</th>" for h in STOCK_TABLE_HEADERS)

    now_desc = timestamp_str or datetime.now(TZ_SH).strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>存量查询</title>
</head>
<body style="font-family:sans-serif;margin:16px;">
  <p style="font-size:14px;color:#555;margin-bottom:12px;">
    系统自动推送：畅捷通 T+ 成品库现存量快照（同步时间：{html.escape(now_desc)}）
  </p>
  <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px;text-align:left;min-width:600px;">
    <thead>
      <tr style="background:#f2f2f2;font-weight:bold;">
        {headers_th}
      </tr>
    </thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>
</body>
</html>"""


def build_home_stock_notification(
    items: list[dict[str, Any]],
    date_str: str | None = None,
) -> tuple[Notification, str]:
    """构造包含「等待您查看」关键词的 Notification 对象。"""
    now_dt = datetime.now(TZ_SH)
    cur_date = date_str or now_dt.strftime("%Y-%m-%d")
    timestamp_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    # 规范标题，满足 020 KEYWORDS["waiting"] == "等待您查看"
    subject = f"【畅捷通存量】{cur_date} 家里库存存量查询（等待您查看）"

    html_table = render_stock_html_table(items, timestamp_str)

    notification = Notification(
        source="tplus-sync-worker",
        event="home_stock_daily_push",
        level="info",
        title=subject,
        summary=f"成品库现存量共 {len(items)} 条物料，同步时间：{timestamp_str}",
        channel_payloads={
            "email": NotifyChannelPayload(
                msg_type="html",
                content={"html": html_table},
            )
        },
        occurred_at=now_dt,
    )
    return notification, subject


def push_home_stock_email(
    rows: list[dict[str, Any]],
    recipients: list[str],
    *,
    dry_run: bool = False,
    date_str: str | None = None,
    smtp_client_factory: Any = None,
) -> dict[str, Any]:
    """执行成品库筛选、HTML 渲染与邮件投递。"""
    finished_items = filter_finished_stock(rows)
    if not finished_items:
        raise ValueError("筛选后成品库记录为 0 条，为防止误覆盖拒绝推送！")

    notification, subject = build_home_stock_notification(finished_items, date_str=date_str)

    if dry_run:
        logger.info("[dry-run] 已生成成品库邮件（不真实发信）：title=%s, rows=%d", subject, len(finished_items))
        return {
            "status": "dry-run",
            "subject": subject,
            "count": len(finished_items),
            "recipients": recipients,
        }

    # 通过统一消息中枢 email 通道投递
    email_channel.send(
        notification,
        target={"to": recipients, "subject": subject},
        smtp_client_factory=smtp_client_factory,
    )
    logger.info("成品库家里库存邮件发送成功：title=%s, recipients=%s", subject, recipients)
    return {
        "status": "sent",
        "subject": subject,
        "count": len(finished_items),
        "recipients": recipients,
    }
