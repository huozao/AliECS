#!/usr/bin/env python3
"""把四处旧告警的收件人搬进 notify_routes。

收件人是生产标识，不进仓库（同 db/seeds/wecom_doc_sources.sql 的做法），所以这里
从既有环境变量读——那四个 env 在收敛前就是各自的投递目标，值不变，收件人也就不变。

在能连到生产库、且 env 已渲染的机器上跑一次：

    python3 scripts/seed_notify_routes.py            # 预演，只打印
    python3 scripts/seed_notify_routes.py --apply    # 真正写入

重复跑是安全的：同一 (source, event, channel, target) 只会存在一条。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg

# (source_key, event_pattern, min_level, 收件人 env, 说明)
FEISHU_ROUTES = [
    ("gold-spread-monitor", "*", "info", "GOLD_SPREAD_FEISHU_RECEIVE_ID", "黄金价差告警与历史回溯"),
    ("aliecs-versions", "weekly_digest", "info", "VERSION_DIGEST_FEISHU_RECEIVE_ID", "版本周报"),
    ("doc-sync", "sync_alert", "info", "SYNC_ALERT_CHAT_ID", "同步告警"),
    ("tplus", "parent_match", "info", "TPLUS_PARENT_MATCH_CHAT_ID", "T+ 父件核对"),
]

FEISHU_PROFILE = os.getenv("NOTIFY_FEISHU_PROFILE", "COMPANY_A")


def planned_rows() -> tuple[list[tuple], list[str]]:
    rows, skipped = [], []
    for source, event, min_level, env_name, note in FEISHU_ROUTES:
        receive_id = (os.getenv(env_name) or "").strip()
        if not receive_id:
            skipped.append(f"{source}/{event}：{env_name} 为空，跳过")
            continue
        target = {
            "profile": FEISHU_PROFILE,
            "receive_id": receive_id,
            "receive_id_type": "chat_id",
        }
        rows.append((source, event, min_level, "feishu", json.dumps(target, ensure_ascii=False), note))
    return rows, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写入；缺省只预演")
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        print("缺少 DATABASE_URL", file=sys.stderr)
        return 2

    rows, skipped = planned_rows()
    for line in skipped:
        print(f"[跳过] {line}")
    if not rows:
        print("没有可写入的路由——四个收件人 env 都是空的，检查 env 是否已渲染。")
        return 1

    for source, event, min_level, channel, target, note in rows:
        # 收件人本身是生产标识，只打印尾部几位够对账即可
        tail = json.loads(target)["receive_id"][-6:]
        print(f"[计划] {source}/{event} → {channel}(…{tail}) min_level={min_level}  {note}")

    if not args.apply:
        print("\n预演结束。确认无误后加 --apply 写入。")
        return 0

    written = 0
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            for source, event, min_level, channel, target, note in rows:
                cur.execute(
                    """
                    SELECT id FROM notify_routes
                    WHERE source_key = %s AND event_pattern = %s
                      AND channel = %s AND target_json = %s::jsonb
                    """,
                    (source, event, channel, target),
                )
                if cur.fetchone():
                    print(f"[已存在] {source}/{event}")
                    continue
                cur.execute(
                    """
                    INSERT INTO notify_routes
                        (source_key, event_pattern, min_level, channel, target_json, note)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (source, event, min_level, channel, target, note),
                )
                written += 1
                print(f"[写入] {source}/{event}")
        conn.commit()
    print(f"\n完成，新增 {written} 条。改路由不需要重启：投递时每次都读表。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
