from __future__ import annotations

import argparse
import json
import sys

from app.pipelines.backfill_smartsheet_images import run_backfill_images, run_backfill_probe
from app.pipelines.group_message_listener import run_group_listener
from app.pipelines.sync_feishu_full import run_sync_feishu_full
from app.pipelines.sync_wecom_full import run_pending_sync_requests, run_sync_wecom_full, run_sync_wecom_source
from app.pipelines.worker_loop import run_worker_loop
from app.pipelines.wecom_structure_backup import (
    bootstrap_structure_backup,
    run_enqueue_daily_structure_backup_jobs,
    run_pending_structure_backup_jobs,
)


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

    backfill_parser = subparsers.add_parser("backfill-images", help="回填企业微信智能表格图片列")
    backfill_parser.add_argument("--profiles", default="", help="逗号分隔的企业微信公司配置，例如 COMPANY_B。")
    backfill_parser.add_argument("--dry-run", action="store_true", help="只读取和下载图片，不写入智能表格。")

    probe_parser = subparsers.add_parser("backfill-images-probe", help="审批附件图片回填探针")
    probe_parser.add_argument("--profiles", default="", help="逗号分隔的企业微信公司配置，例如 COMPANY_B。")
    probe_parser.add_argument("--sp-no", required=True, help="审批单号，例如 202603240010。")
    probe_parser.add_argument("--dry-run", action="store_true", help="只读取审批和下载图片，不写入智能表格。")

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-wecom-structure-backup",
        help="在企微A创建或校验智能表格结构备份文档",
    )
    bootstrap_parser.add_argument("--profile", default="", help="创建文档所用企业配置，默认 COMPANY_A")
    bootstrap_parser.add_argument("--docid", default="", help="校验已有备份文档；为空则新建")

    backup_parser = subparsers.add_parser(
        "sync-wecom-structure-backup",
        help="为全部企微智能表格生成并消费结构备份任务",
    )
    backup_parser.add_argument("--limit", type=int, default=1000, help="本次最多消费多少条备份任务")

    backup_pending_parser = subparsers.add_parser(
        "consume-structure-backup-jobs",
        help="消费待处理的企微结构备份任务",
    )
    backup_pending_parser.add_argument("--limit", type=int, default=10, help="本次最多消费多少条备份任务")

    listener_parser = subparsers.add_parser("group-listener", help="常驻：长连接接收群@消息并绑定/入库")
    listener_parser.add_argument("--profiles", default="", help="企业微信公司配置，默认取 WECOM_ENV_PROFILES 第一个。")
    listener_parser.add_argument("--max-seconds", type=float, default=None, help="运行时长上限（秒），调试用；默认常驻。")

    subparsers.add_parser("run-loop", help="常驻循环：周期全量 + 轮询消费手动同步请求")

    args = parser.parse_args(argv)
    if args.command == "sync-wecom-full":
        return run_sync_wecom_full(profiles_arg=args.profiles)
    if args.command == "sync-feishu-full":
        return run_sync_feishu_full(profiles_arg=args.profiles)
    if args.command == "sync-wecom-source":
        return run_sync_wecom_source(source_id=args.source_id)
    if args.command == "consume-sync-requests":
        return run_pending_sync_requests(limit=args.limit)
    if args.command == "backfill-images":
        return run_backfill_images(profiles_arg=args.profiles, dry_run=args.dry_run).exit_code
    if args.command == "backfill-images-probe":
        return run_backfill_probe(sp_no=args.sp_no, profiles_arg=args.profiles, dry_run=args.dry_run)
    if args.command == "bootstrap-wecom-structure-backup":
        result = bootstrap_structure_backup(profile=args.profile, docid=args.docid)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "sync-wecom-structure-backup":
        run_enqueue_daily_structure_backup_jobs(force=True)
        return run_pending_structure_backup_jobs(limit=args.limit, force=True)
    if args.command == "consume-structure-backup-jobs":
        return run_pending_structure_backup_jobs(limit=args.limit, force=True)
    if args.command == "group-listener":
        run_group_listener(profiles_arg=args.profiles, max_seconds=args.max_seconds)
        return 0
    if args.command == "run-loop":
        return run_worker_loop()

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
