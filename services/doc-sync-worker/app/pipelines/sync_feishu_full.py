from __future__ import annotations

from typing import Any

from app.providers.feishu import (
    FeishuBitableClient,
    FeishuBitableSource,
    credentials_for_profile,
    discover_profile_sources,
    env_profiles,
    session_console_bootstrap_config,
)
from app.pipelines.managed_contacts import sync_managed_contact_from_row
from app.storage.postgres import build_record_snapshot, compose_source_name, open_store


FIELD_TEXT = 1
FIELD_NUMBER = 2
FIELD_DATETIME = 5
FIELD_CHECKBOX = 7
FIELD_URL = 15


def _field(name: str, field_type: int = FIELD_TEXT) -> dict[str, Any]:
    return {"field_name": name, "type": field_type}


SESSION_CONSOLE_TABLE_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "会话索引表",
        "fields": [
            _field("会话编号"),
            _field("会话名称"),
            _field("会话类型"),
            _field("session_key"),
            _field("关联用户"),
            _field("关联群"),
            _field("飞书用户名"),
            _field("飞书群名"),
            _field("ChatGPT 项目名"),
            _field("ChatGPT 对话标题"),
            _field("ChatGPT 项目首页链接", FIELD_URL),
            _field("ChatGPT 对话链接", FIELD_URL),
            _field("会话状态"),
            _field("是否当前会话", FIELD_CHECKBOX),
            _field("会话版本", FIELD_NUMBER),
            _field("上下文摘要"),
            _field("系统提示词 / 角色设定"),
            _field("回复风格"),
            _field("创建时间", FIELD_DATETIME),
            _field("最近活跃时间", FIELD_DATETIME),
            _field("消息数量", FIELD_NUMBER),
            _field("@机器人次数", FIELD_NUMBER),
            _field("备注"),
        ],
    },
    {
        "name": "消息日志表",
        "fields": [
            _field("日志编号"),
            _field("飞书 message_id"),
            _field("event_id"),
            _field("tenant_key"),
            _field("消息时间", FIELD_DATETIME),
            _field("接收时间", FIELD_DATETIME),
            _field("聊天类型"),
            _field("关联用户"),
            _field("关联群"),
            _field("发送人 open_id"),
            _field("发送人名称"),
            _field("群 chat_id"),
            _field("群名称"),
            _field("消息类型"),
            _field("原始消息内容"),
            _field("清洗后内容"),
            _field("是否 @ 机器人", FIELD_CHECKBOX),
            _field("@对象列表"),
            _field("是否命令", FIELD_CHECKBOX),
            _field("命令类型"),
            _field("是否需要送 ChatGPT", FIELD_CHECKBOX),
            _field("不处理原因"),
            _field("匹配会话"),
            _field("处理状态"),
            _field("是否已回复飞书", FIELD_CHECKBOX),
            _field("飞书回复 message_id"),
            _field("原始事件 JSON"),
            _field("附件链接", FIELD_URL),
            _field("错误信息"),
        ],
    },
    {
        "name": "回复任务表",
        "fields": [
            _field("任务编号"),
            _field("关联消息"),
            _field("关联会话"),
            _field("任务类型"),
            _field("任务状态"),
            _field("ChatGPT 对话链接", FIELD_URL),
            _field("给 ChatGPT 的输入"),
            _field("ChatGPT 回复内容"),
            _field("是否需要人工审核", FIELD_CHECKBOX),
            _field("审核状态"),
            _field("处理人"),
            _field("处理开始时间", FIELD_DATETIME),
            _field("处理完成时间", FIELD_DATETIME),
            _field("发送结果"),
            _field("失败原因"),
            _field("备注"),
        ],
    },
    {
        "name": "群表",
        "fields": [
            _field("群编号"),
            _field("群名称"),
            _field("chat_id"),
            _field("群类型"),
            _field("是否启用机器人", FIELD_CHECKBOX),
            _field("是否记录全量消息", FIELD_CHECKBOX),
            _field("回复模式"),
            _field("默认会话"),
            _field("群负责人"),
            _field("风险级别"),
            _field("最近消息时间", FIELD_DATETIME),
            _field("最近 @ 机器人时间", FIELD_DATETIME),
            _field("备注"),
        ],
    },
    {
        "name": "用户表",
        "fields": [
            _field("用户编号"),
            _field("飞书用户名"),
            _field("open_id"),
            _field("union_id"),
            _field("user_id"),
            _field("用户状态"),
            _field("用户角色"),
            _field("所属部门"),
            _field("默认私聊会话"),
            _field("每日额度", FIELD_NUMBER),
            _field("已用次数", FIELD_NUMBER),
            _field("最近互动时间", FIELD_DATETIME),
            _field("备注"),
        ],
    },
    {
        "name": "规则配置表",
        "fields": [
            _field("规则编号"),
            _field("规则名称"),
            _field("规则对象类型"),
            _field("关联用户"),
            _field("关联群"),
            _field("关联会话"),
            _field("是否启用", FIELD_CHECKBOX),
            _field("是否记录全量消息", FIELD_CHECKBOX),
            _field("回复模式"),
            _field("是否允许图片", FIELD_CHECKBOX),
            _field("是否允许文件", FIELD_CHECKBOX),
            _field("是否需要审核", FIELD_CHECKBOX),
            _field("每日最大请求数", FIELD_NUMBER),
            _field("敏感群标记", FIELD_CHECKBOX),
            _field("默认处理人"),
            _field("备注"),
        ],
    },
]


def _table_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("table_name") or "").strip()


def _table_id(item: dict[str, Any]) -> str:
    return str(item.get("table_id") or item.get("id") or "").strip()


def _persisted_feishu_sources(store: Any, profile: str) -> list[FeishuBitableSource]:
    if not hasattr(store, "list_bitable_sources"):
        return []
    rows = store.list_bitable_sources("feishu", profile)
    sources: list[FeishuBitableSource] = []
    for row in rows:
        app_token = str(row.get("external_doc_id") or "")
        table_id = str(row.get("external_sheet_id") or "")
        if not app_token or not table_id:
            continue
        sheet_name = str(row.get("sheet_name") or row.get("source_name") or table_id)
        document_name = str(row.get("document_name") or row.get("source_name") or "")
        sources.append(
            FeishuBitableSource(
                env_profile=profile,
                app_token=app_token,
                table_id=table_id,
                source_name=sheet_name,
                source_url=str(row.get("source_url") or ""),
                document_name=document_name,
                sheet_name=sheet_name,
            )
        )
    return sources


def _merge_feishu_sources(
    env_sources: list[FeishuBitableSource],
    persisted_sources: list[FeishuBitableSource],
) -> list[FeishuBitableSource]:
    sources: list[FeishuBitableSource] = []
    seen: set[tuple[str, str, str]] = set()
    for source in [*env_sources, *persisted_sources]:
        key = (source.app_token or source.wiki_node_token, source.table_id, source.source_name)
        if key in seen:
            continue
        seen.add(key)
        sources.append(source)
    return sources


def bootstrap_session_console_sources(
    store: Any,
    client: FeishuBitableClient,
    profile: str,
    app_name: str,
    folder_token: str = "",
) -> list[FeishuBitableSource]:
    app = client.create_app(app_name, folder_token=folder_token)
    existing_tables = {
        _table_name(item): _table_id(item)
        for item in client.list_tables(app.app_token)
        if _table_name(item) and _table_id(item)
    }

    sources: list[FeishuBitableSource] = []
    for schema in SESSION_CONSOLE_TABLE_SCHEMAS:
        table_name = str(schema["name"])
        table_id = existing_tables.get(table_name, "")
        if not table_id:
            table = client.create_table(app.app_token, table_name, fields=list(schema["fields"]))
            table_id = table.table_id
        store.ensure_source(
            provider="feishu",
            env_profile=profile,
            source_name=compose_source_name(app_name, table_name),
            source_type="bitable_table",
            external_doc_id=app.app_token,
            external_sheet_id=table_id,
            source_url=app.url,
            document_name=app_name,
            sheet_name=table_name,
        )
        sources.append(
            FeishuBitableSource(
                env_profile=profile,
                app_token=app.app_token,
                table_id=table_id,
                source_name=table_name,
                source_url=app.url,
                document_name=app_name,
                sheet_name=table_name,
            )
        )
    return sources


def _resolve_app_token(client: FeishuBitableClient, source: FeishuBitableSource) -> str:
    if source.app_token:
        return source.app_token
    if source.wiki_node_token:
        return client.resolve_app_token_from_wiki_node(source.wiki_node_token)
    raise RuntimeError(f"{source.source_name} 缺少 FEISHU_APP_TOKEN 或 FEISHU_WIKI_NODE_TOKEN。")


def _rescan_app_tables(
    store: Any,
    client: FeishuBitableClient,
    profile: str,
    app_token: str,
    document_name: str,
    source_url: str = "",
    view_ids: dict[str, str] | None = None,
) -> tuple[list[tuple[int, FeishuBitableSource]], int]:
    """整簿重扫：列出工作簿全部数据表并登记为同步源，返回 ([(source_id, source), ...], 停用数)。
    新表自动收录；本轮没看到的表标记 disabled（list_tables 返回空时不剪，防误伤）。"""
    doc_name = str(document_name or app_token)
    views = view_ids or {}
    pairs: list[tuple[int, FeishuBitableSource]] = []
    seen_table_ids: list[str] = []
    for item in client.list_tables(app_token):
        table_id = _table_id(item)
        if not table_id:
            continue
        sheet_name = _table_name(item) or table_id
        seen_table_ids.append(table_id)
        source = FeishuBitableSource(
            env_profile=profile,
            app_token=app_token,
            table_id=table_id,
            source_name=sheet_name,
            view_id=views.get(table_id, ""),
            source_url=source_url,
            document_name=doc_name,
            sheet_name=sheet_name,
        )
        source_id = store.ensure_source(
            provider="feishu",
            env_profile=profile,
            source_name=compose_source_name(doc_name, sheet_name),
            source_type="bitable_table",
            external_doc_id=app_token,
            external_sheet_id=table_id,
            source_url=source_url,
            document_name=doc_name,
            sheet_name=sheet_name,
        )
        pairs.append((source_id, source))
    disabled = store.disable_missing_sheets("feishu", profile, app_token, seen_table_ids)
    return pairs, disabled


def ensure_bitable_app_anchor(
    store: Any,
    profile: str,
    app_token: str,
    source: FeishuBitableSource,
) -> int:
    document_name = source.document_name or source.source_name or app_token
    return store.upsert_structure_document(
        provider="feishu",
        env_profile=profile,
        source_type="bitable_app",
        external_doc_id=app_token,
        document_name=document_name,
        source_url=source.source_url,
    )


def _sync_bitable_records(
    store: Any,
    client: FeishuBitableClient,
    source_id: int,
    app_token: str,
    table_id: str,
    view_id: str,
    counts: dict[str, int],
    source_name: str = "",
) -> None:
    fields = client.list_fields(app_token, table_id)
    field_titles = store.replace_fields(source_id, fields)
    records_response = client.get_records(app_token, table_id, view_id=view_id)
    records = records_response.get("records") or []
    counts["sheet_count"] += 1
    counts["record_count"] += len(records)
    print(
        f"[飞书同步] table_id={table_id} 完整拉取 {len(records)} 条，"
        f"分页 {records_response.get('page_count', 1)} 页。"
    )
    for record in records:
        if not isinstance(record, dict):
            continue
        snapshot = build_record_snapshot(record, field_titles)
        decision = store.upsert_record(source_id, snapshot)
        if sync_managed_contact_from_row(store, source_name, snapshot.normalized_json):
            counts["managed_contact_count"] = counts.get("managed_contact_count", 0) + 1
        if decision.action == "create":
            counts["created_count"] += 1
        elif decision.action == "update":
            counts["updated_count"] += 1
    store.mark_source_synced(source_id)


def run_sync_feishu_full(profiles_arg: str = "") -> int:
    profiles = env_profiles(profiles_arg)
    if not profiles:
        print("未配置 FEISHU_ENV_PROFILES，也未传入 --profiles。示例：COMPANY_A,COMPANY_B")
        return 1

    store = open_store()
    exit_code = 0
    try:
        for profile in profiles:
            print(f"[飞书同步] 开始处理公司配置：{profile}")
            run_id = store.start_run(provider="feishu", env_profile=profile, mode="full")
            counts = {
                "source_count": 0,
                "sheet_count": 0,
                "record_count": 0,
                "created_count": 0,
                "updated_count": 0,
                "error_count": 0,
            }
            errors: list[dict[str, Any]] = []
            try:
                credentials = credentials_for_profile(profile)
                sources = _merge_feishu_sources(
                    discover_profile_sources(profile),
                    _persisted_feishu_sources(store, profile),
                )
                if not credentials:
                    raise RuntimeError(f"{profile} 缺少 FEISHU_{profile}_APP_ID 或 FEISHU_{profile}_APP_SECRET。")
                credential = credentials[0]
                client = FeishuBitableClient(
                    app_id=credential.app_id,
                    app_secret=credential.app_secret,
                    api_base=credential.api_base,
                )
                if not sources:
                    bootstrap_config = session_console_bootstrap_config(profile)
                    if bootstrap_config.enabled:
                        sources = bootstrap_session_console_sources(
                            store,
                            client,
                            profile,
                            app_name=bootstrap_config.app_name,
                            folder_token=bootstrap_config.folder_token,
                        )
                counts["source_count"] = len(sources)
                if not sources:
                    raise RuntimeError(
                        f"{profile} 未配置 FEISHU_{profile}_APP_TOKEN/TABLE_ID 或 WIKI_NODE_TOKEN，"
                        f"数据库也没有已登记 Bitable source；如需自动创建会话管理台，"
                        f"设置 FEISHU_{profile}_SESSION_CONSOLE_BOOTSTRAP=true 后运行一次同步。"
                    )

                for source in sources:
                    app_token = _resolve_app_token(client, source)
                    document_name = source.document_name or source.source_name
                    sheet_name = source.sheet_name or source.table_id
                    ensure_bitable_app_anchor(store, profile, app_token, source)
                    source_id = store.ensure_source(
                        provider="feishu",
                        env_profile=profile,
                        source_name=compose_source_name(document_name, sheet_name),
                        source_type="bitable_table",
                        external_doc_id=app_token,
                        external_sheet_id=source.table_id,
                        source_url=source.source_url,
                        document_name=document_name,
                        sheet_name=sheet_name,
                    )
                    _sync_bitable_records(
                        store,
                        client,
                        source_id,
                        app_token,
                        source.table_id,
                        source.view_id,
                        counts,
                        source_name=sheet_name,
                    )
                status = "success" if counts["error_count"] == 0 else "partial_failed"
            except Exception as exc:  # noqa: BLE001 - worker should persist one run row with diagnostics.
                exit_code = 1
                counts["error_count"] += 1
                errors.append({"env_profile": profile, "error": str(exc)})
                status = "failed"
                print(f"[飞书同步] {profile} 同步失败：{exc}")

            store.finish_run(run_id, status=status, counts=counts, error_json=errors)
            print(
                f"[飞书同步] {profile} 完成：status={status} "
                f"tables={counts['sheet_count']} records={counts['record_count']} "
                f"created={counts['created_count']} updated={counts['updated_count']} errors={counts['error_count']}"
            )
            if counts["error_count"]:
                exit_code = 1
    finally:
        store.close()

    return exit_code
