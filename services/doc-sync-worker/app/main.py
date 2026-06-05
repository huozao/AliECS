from __future__ import annotations

import argparse
import sys

from app.pipelines.sync_feishu_full import run_sync_feishu_full
from app.pipelines.sync_wecom_full import run_pending_sync_requests, run_sync_wecom_full, run_sync_wecom_source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AliECS 文档同步 worker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync-wecom-full", help="完整同步企业微信智能表格")
    sync_parser.add_argument(
        "--profiles",
        default="",
        help="逗号分隔的企业微信公司配置，例如 COMPANY_A,COMPANY_B；为空时读取 WECOM_ENV_PROFILES。",
    )
    feishu_parser = subparsers.add_parser("sync-feishu-full", help="完整同步飞书多维表格")
    feishu_parser.add_argument(
        "--profiles",
        default="",
        help="逗号分隔的飞书公司配置，例如 COMPANY_A,COMPANY_B；为空时读取 FEISHU_ENV_PROFILES。",
    )

    source_parser = subparsers.add_parser("sync-wecom-source", help="手动同步一个已登记的企业微信表格 source_id")
    source_parser.add_argument("--source-id", type=int, required=True, help="external_sources.id")

    pending_parser = subparsers.add_parser("consume-sync-requests", help="消费后台创建的手动同步请求")
    pending_parser.add_argument("--limit", type=int, default=10, help="本次最多处理多少条 pending 请求")

    args = parser.parse_args(argv)
    if args.command == "sync-wecom-full":
        return run_sync_wecom_full(profiles_arg=args.profiles)
    if args.command == "sync-feishu-full":
        return run_sync_feishu_full(profiles_arg=args.profiles)
    if args.command == "sync-wecom-source":
        return run_sync_wecom_source(source_id=args.source_id)
    if args.command == "consume-sync-requests":
        return run_pending_sync_requests(limit=args.limit)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
