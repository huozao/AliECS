from __future__ import annotations

from typing import Any

from app.providers.feishu import (
    FeishuBitableClient,
    FeishuBitableSource,
    credentials_for_profile,
    discover_profile_sources,
    env_profiles,
)
from app.storage.postgres import build_record_snapshot, compose_source_name, open_store


def _resolve_app_token(client: FeishuBitableClient, source: FeishuBitableSource) -> str:
    if source.app_token:
        return source.app_token
    if source.wiki_node_token:
        return client.resolve_app_token_from_wiki_node(source.wiki_node_token)
    raise RuntimeError(f"{source.source_name} 缺少 FEISHU_APP_TOKEN 或 FEISHU_WIKI_NODE_TOKEN。")


def _sync_bitable_records(
    store: Any,
    client: FeishuBitableClient,
    source_id: int,
    app_token: str,
    table_id: str,
    view_id: str,
    counts: dict[str, int],
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
        decision = store.upsert_record(source_id, build_record_snapshot(record, field_titles))
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
                sources = discover_profile_sources(profile)
                counts["source_count"] = len(sources)
                if not credentials:
                    raise RuntimeError(f"{profile} 缺少 FEISHU_{profile}_APP_ID 或 FEISHU_{profile}_APP_SECRET。")
                if not sources:
                    raise RuntimeError(f"{profile} 未配置 FEISHU_{profile}_APP_TOKEN/TABLE_ID 或 WIKI_NODE_TOKEN。")

                credential = credentials[0]
                client = FeishuBitableClient(
                    app_id=credential.app_id,
                    app_secret=credential.app_secret,
                    api_base=credential.api_base,
                )
                for source in sources:
                    app_token = _resolve_app_token(client, source)
                    source_id = store.ensure_source(
                        provider="feishu",
                        env_profile=profile,
                        source_name=compose_source_name(source.source_name, source.table_id),
                        source_type="bitable_table",
                        external_doc_id=app_token,
                        external_sheet_id=source.table_id,
                        source_url=source.source_url,
                        document_name=source.source_name,
                        sheet_name=source.table_id,
                    )
                    _sync_bitable_records(store, client, source_id, app_token, source.table_id, source.view_id, counts)
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
