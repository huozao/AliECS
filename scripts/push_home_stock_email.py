#!/usr/bin/env python3
"""畅捷通成品库家里库存邮件推送脚本。

用途：
每天定时或按需从畅捷通 T+ ERP 获取成品库现存量，生成标准 HTML 表格，
并通过统一消息中枢（app.notify.channels.email）发送邮件（主题含“等待您查看”），
供 meidi-auto 对账流水线与管理人员无缝消费。

用法：
    # 预演模式（拉取并渲染，打印统计，不真正发信）
    python3 scripts/push_home_stock_email.py --dry-run

    # 正式发送
    python3 scripts/push_home_stock_email.py

    # 指定收件人
    python3 scripts/push_home_stock_email.py --recipients "user1@example.com,user2@example.com"
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "services" / "backend-api"
WORKER_ROOT = REPO_ROOT / "services" / "tplus-sync-worker"

for p in (str(BACKEND_ROOT), str(WORKER_ROOT / "src"), str(WORKER_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tplus_datahub.modules.inventory.push_home_stock import push_home_stock_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("push_home_stock")


def _resolve_recipients(cli_arg: str | None) -> list[str]:
    if cli_arg:
        raw = cli_arg
    else:
        raw = os.getenv("RECIPIENT_EMAILS") or os.getenv("EMAIL_ADDRESS_QQ") or ""

    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    return parts


def fetch_current_stock_rows() -> list[dict]:
    """从 T+ OpenAPI 或本地最新现存量导出中拉取原始行。"""
    try:
        from tplus_datahub.modules.current_stock.sync_current_stock import sync_current_stock

        logger.info("正在通过畅捷通官方现存量报表引擎 (ST_CurrentStockRpt) 查询现存量...")
        rows = sync_current_stock()
        logger.info("畅捷通现存量报表查询成功，获取到 %d 条记录", len(rows))
        return rows
    except Exception as exc:
        logger.warning("通过 ST_CurrentStockRpt 查询失败 (%s)，尝试回退至基础档案接口...", exc)

    try:
        from config.endpoints import VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS
        from tplus_datahub.modules.base_archive.sync_base_archive import sync_base_archive

        endpoint = VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS["current_stock"]
        logger.info("正在通过畅捷通 OpenAPI 基础接口查询现存量...")
        rows = sync_base_archive(
            module_name="current_stock",
            endpoint=endpoint,
        )
        logger.info("畅捷通基础现存量查询成功，获取到 %d 条记录", len(rows))
        return rows
    except Exception as exc:
        logger.warning("通过基础 OpenAPI 直连获取失败 (%s)，尝试回退至最新导出文件...", exc)

    # 回退尝试读取本地最新 export 文件
    try:
        from app.routers.exports import _latest_tplus_export_file
        import pandas as pd

        excel_path = _latest_tplus_export_file("current_stock")
        if excel_path and excel_path.is_file():
            logger.info("找到本地最新现存量快照: %s", excel_path)
            df = pd.read_excel(excel_path, dtype=str).fillna("")
            return df.to_dict(orient="records")
    except Exception as fallback_exc:
        logger.warning("回退读取本地快照亦失败: %s", fallback_exc)

    raise RuntimeError("无法获取畅捷通现存量数据（OpenAPI 与本地快照均不可用）")


def main() -> int:
    parser = argparse.ArgumentParser(description="推送畅捷通成品库家里库存邮件")
    parser.add_argument("--dry-run", action="store_true", help="预演模式：只处理与渲染，不发送真实邮件")
    parser.add_argument("--mock", action="store_true", help="本地演练：使用示例现存量数据模拟，不依赖 T+ OpenAPI")
    parser.add_argument("--recipients", type=str, default="", help="收件人邮箱（逗号分隔）")
    parser.add_argument("--output", type=str, default="", help="可选将生成的 HTML 保存到指定路径")
    args = parser.parse_args()

    recipients = _resolve_recipients(args.recipients)
    if not recipients and not args.dry_run:
        logger.error("未指定收件人，请通过 --recipients 或环境变量 RECIPIENT_EMAILS / EMAIL_ADDRESS_QQ 配置。")
        return 1

    try:
        if args.mock:
            logger.info("使用本地模拟现存量数据演练...")
            rows = [
                {"WarehouseCode": "001", "WarehouseName": "原材库", "InventoryCode": "10001", "InventoryName": "原料A", "ExistingQuantity": "200"},
                {"WarehouseCode": "002", "WarehouseName": "成品库", "InventoryCode": "30122", "InventoryName": "30122-BX3027海灰色母", "Specification": "PP", "UnitName": "kg", "ExistingQuantity": "120.5"},
                {"WarehouseCode": "002", "WarehouseName": "成品库", "InventoryCode": "20005", "InventoryName": "20005-黑色母料", "Specification": "PE", "UnitName": "包", "ExistingQuantity": "80"},
            ]
        else:
            rows = fetch_current_stock_rows()
    except Exception as e:
        logger.error("获取库存失败: %s", e)
        return 2

    try:
        res = push_home_stock_email(rows, recipients, dry_run=args.dry_run)
        logger.info("处理完成: %s", res)

        if args.output:
            from tplus_datahub.modules.inventory.push_home_stock import (
                build_home_stock_notification,
                filter_finished_stock,
            )
            finished = filter_finished_stock(rows)
            notif, _ = build_home_stock_notification(finished)
            html_content = notif.channel_payloads["email"].content["html"]
            Path(args.output).write_text(html_content, encoding="utf-8")
            logger.info("已将 HTML 表格快照导出至: %s", args.output)

        return 0
    except Exception as e:
        logger.error("推送失败: %s", e)
        return 3


if __name__ == "__main__":
    sys.exit(main())
