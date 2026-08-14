from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable

from psycopg.types.json import Jsonb


GROUPS = (
    ("tplus", "T+ ERP"),
    ("wecom_company_a", "企微A"),
    ("wecom_company_b", "企微B"),
    ("feishu", "飞书"),
)
WECOM_SYNC_TYPES = {"smartsheet_doc", "registry_doc"}
SYSTEM_SOURCE_TYPE = "structure_backup_doc"


class InvalidLocatorAction(ValueError):
    pass


def valid_wecom_docid(value: str) -> bool:
    return value.startswith("dc") and len(value) >= 80


def source_group(provider: str, env_profile: str) -> str:
    if provider == "chanjet":
        return "tplus"
    if provider == "feishu":
        return "feishu"
    return f"wecom_{str(env_profile or '').lower()}"


ASSET_SQL = """
SELECT
    l.provider,
    l.env_profile,
    COALESCE(l.api_doc_id, l.share_ref, ''),
    COALESCE(NULLIF(d.source_type, ''), l.source_kind),
    COALESCE(NULLIF(l.document_name, ''), NULLIF(d.document_name, ''), NULLIF(d.source_name, ''), '未命名文档') AS document_name,
    d.source_name,
    l.external_source_id,
    COUNT(DISTINCT s.id) FILTER (
        WHERE s.status = 'active'
          AND ((s.provider = 'wecom' AND s.source_type = 'smartsheet_sheet')
            OR (s.provider = 'feishu' AND s.source_type = 'bitable_table'))
    ) AS sheet_count,
    COUNT(DISTINCT j.id) FILTER (WHERE j.enabled) AS job_count,
    MAX(s.last_sync_at) FILTER (WHERE s.status = 'active') AS last_sync_at,
    l.syncability_status,
    l.capabilities,
    l.lifecycle_status
FROM document_locator_registry l
LEFT JOIN external_sources d ON d.id = l.external_source_id
LEFT JOIN external_sources s
  ON s.provider = l.provider
 AND s.env_profile = l.env_profile
 AND s.external_doc_id = l.api_doc_id
 AND s.external_sheet_id <> ''
LEFT JOIN sync_jobs j ON j.source_id = s.id AND j.kind = 'pull'
WHERE l.lifecycle_status IN ('active', 'unresolved')
GROUP BY l.id, d.id
ORDER BY l.provider, l.env_profile, l.id
"""


def _doc_capabilities(
    provider: str,
    source_type: str,
    external_doc_id: str,
    sheet_count: int,
    syncability_status: str,
    locator_capabilities: dict[str, Any],
    lifecycle_status: str,
    source_id: int | None,
) -> dict[str, Any]:
    system_managed = source_type == SYSTEM_SOURCE_TYPE
    readable = syncability_status == "verified" and locator_capabilities.get("read") == "verified"
    active = lifecycle_status == "active" and source_id is not None
    if provider == "wecom":
        resolved = valid_wecom_docid(external_doc_id)
        syncable = resolved and active and readable and source_type in WECOM_SYNC_TYPES and not system_managed
        reason = "" if resolved else "缺少有效企微 docid"
    elif provider == "feishu":
        resolved = bool(external_doc_id)
        syncable = resolved and active and readable and source_type == "bitable_app"
        reason = "" if resolved else "缺少有效飞书文档标识"
    else:
        resolved = False
        syncable = False
        reason = "不支持的同步来源"
    if system_managed:
        reason = "系统管理资产，仅提供下载"
    elif syncability_status == "permission-denied":
        reason = "权限验证失败"
    elif resolved and not readable:
        reason = "文档尚未通过读取验证"
    return {
        "can_sync": syncable,
        "can_download": resolved and readable and (system_managed or sheet_count > 0),
        "can_copy": (
            provider == "wecom" and resolved and active and readable and not system_managed
            and locator_capabilities.get("copy") in {"allowed", "verified"}
        ),
        "system_managed": system_managed,
        "reason": reason,
    }


def asset_catalog(conn: Any, *, tplus_items: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {key: {"key": key, "title": title, "items": []} for key, title in GROUPS}
    groups["tplus"]["items"] = [
        {
            **dict(item),
            "download_url": (
                f"/v1/sync/exports/tplus/{str(item.get('download_url') or '').rsplit('/', 1)[-1]}"
                if item.get("download_url") else None
            ),
            "job_key": "chanjet.full",
            "can_sync": True,
            "can_download": bool(item.get("download_url")),
            "can_copy": False,
            "system_managed": False,
            "reason": "",
        }
        for item in tplus_items
    ]
    with conn.cursor() as cur:
        cur.execute(ASSET_SQL)
        rows = cur.fetchall()
    for row in rows:
        provider = str(row[0] or "")
        env_profile = str(row[1] or "")
        external_doc_id = str(row[2] or "")
        source_type = str(row[3] or "")
        group_key = source_group(provider, env_profile)
        if group_key not in groups:
            continue
        source_id = int(row[6]) if row[6] is not None else None
        sheet_count = int(row[7] or 0)
        locator_capabilities = row[11] if isinstance(row[11], dict) else {}
        capabilities = _doc_capabilities(
            provider, source_type, external_doc_id, sheet_count,
            str(row[10] or ""), locator_capabilities, str(row[12] or ""), source_id,
        )
        item = {
            "name": str(row[4] or row[5] or "未命名文档"),
            "source_id": source_id,
            "sheets": sheet_count,
            "jobs": int(row[8] or 0),
            "updated_at": row[9],
            **capabilities,
            "syncable": capabilities["can_sync"],
            "download_url": f"/v1/sync/assets/{source_id}/download" if capabilities["can_download"] else None,
        }
        groups[group_key]["items"].append(item)
    return {"groups": list(groups.values())}


def _source_row(cur: Any, source_id: int) -> tuple[Any, ...]:
    cur.execute(
        """
        SELECT provider, env_profile, external_doc_id, source_type,
               COALESCE(NULLIF(document_name, ''), NULLIF(source_name, ''), '未命名文档'), status
        FROM external_sources WHERE id = %s AND external_sheet_id = ''
        """,
        (source_id,),
    )
    row = cur.fetchone()
    if not row:
        raise InvalidLocatorAction("文档资产不存在")
    return row


def _require_copy_capability(cur: Any, source_id: int) -> None:
    cur.execute(
        """
        SELECT syncability_status, capabilities
        FROM document_locator_registry
        WHERE external_source_id = %s AND lifecycle_status = 'active'
        ORDER BY updated_at DESC, id DESC LIMIT 1
        """,
        (source_id,),
    )
    row = cur.fetchone()
    capabilities = row[1] if row and isinstance(row[1], dict) else {}
    if (
        not row
        or str(row[0]) != "verified"
        or capabilities.get("read") != "verified"
        or capabilities.get("copy") not in {"allowed", "verified"}
    ):
        raise InvalidLocatorAction("该资产未通过读取或复制权限验证")


def _register_external_copy(
    conn: Any,
    *,
    copy_request_id: int,
    env_profile: str,
    api_doc_id: str,
    source_url: str,
    document_name: str,
    requested_by: str,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO external_sources(
                provider, env_profile, source_name, source_type, external_doc_id,
                external_sheet_id, source_url, document_name, sheet_name, status, updated_at
            ) VALUES ('wecom', %s, %s, 'smartsheet_doc', %s, '', %s, %s, '', 'active', NOW())
            ON CONFLICT(provider, env_profile, external_doc_id, external_sheet_id)
            DO UPDATE SET source_name = EXCLUDED.source_name, document_name = EXCLUDED.document_name,
                          source_url = EXCLUDED.source_url, status = 'active', updated_at = NOW()
            RETURNING id
            """,
            (env_profile, document_name, api_doc_id, source_url, document_name),
        )
        source_id = int(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO document_locator_registry(
                provider, env_profile, api_doc_id, document_name, source_url, source_kind,
                lifecycle_status, syncability_status, capabilities, external_source_id,
                last_verified_at, updated_at
            ) VALUES ('wecom', %s, %s, %s, %s, 'copy', 'active', 'verified', %s, %s, NOW(), NOW())
            ON CONFLICT(provider, env_profile, api_doc_id) WHERE api_doc_id IS NOT NULL
            DO UPDATE SET document_name = EXCLUDED.document_name, source_url = EXCLUDED.source_url,
                          lifecycle_status = 'active', syncability_status = 'verified',
                          capabilities = EXCLUDED.capabilities, external_source_id = EXCLUDED.external_source_id,
                          locator_version = document_locator_registry.locator_version + 1,
                          last_verified_at = NOW(), updated_at = NOW()
            RETURNING id, locator_version
            """,
            (env_profile, api_doc_id, document_name, source_url, Jsonb({"read": "verified", "write": "verified", "copy": "verified"}), source_id),
        )
        locator_id, locator_version = map(int, cur.fetchone())
        cur.execute(
            """
            INSERT INTO document_locator_events(
                locator_id, locator_version, event_type, trigger_source, changed_fields, status_summary, actor
            ) VALUES (%s, %s, 'copy-created', 'sync-api', %s, %s, %s)
            ON CONFLICT(locator_id, locator_version, event_type) DO NOTHING
            """,
            (locator_id, locator_version, Jsonb(["api_doc_id", "external_source_id"]), Jsonb({"status": "registered"}), requested_by),
        )
        cur.execute(
            """
            INSERT INTO document_locator_mirror_jobs(locator_id, locator_version, trigger, updated_at)
            VALUES (%s, %s, 'copy-created', NOW())
            ON CONFLICT(locator_id, locator_version) DO NOTHING
            """,
            (locator_id, locator_version),
        )
        cur.execute(
            """
            INSERT INTO sync_requests(source_id, provider, env_profile, mode, status, requested_by)
            VALUES (%s, 'wecom', %s, 'manual', 'pending', 'copy-auto') RETURNING id
            """,
            (source_id, env_profile),
        )
        sync_request_id = int(cur.fetchone()[0])
        cur.execute(
            """
            UPDATE document_copy_requests
            SET status = 'registered', locator_id = %s, sync_request_id = %s,
                finished_at = NOW(), updated_at = NOW(), error_kind = '', error_summary = ''
            WHERE id = %s
            """,
            (locator_id, sync_request_id, copy_request_id),
        )
    conn.commit()
    return {
        "status": "registered",
        "copy_request_id": copy_request_id,
        "source_id": source_id,
        "locator_id": locator_id,
        "sync_request_id": sync_request_id,
    }


def copy_asset(
    connect: Callable[[], Any],
    *,
    source_id: int,
    idempotency_key: str,
    requested_by: str,
    copier: Callable[..., dict[str, Any]] | None = None,
    now_name: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise InvalidLocatorAction("缺少有效 idempotency_key")
    with closing(connect()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.status, c.new_api_doc_id, c.locator_id, c.sync_request_id,
                       c.source_id, c.requested_name, r.external_source_id
                FROM document_copy_requests c
                LEFT JOIN document_locator_registry r ON r.id = c.locator_id
                WHERE c.idempotency_key = %s
                """,
                (key,),
            )
            existing = cur.fetchone()
            if existing and int(existing[5]) != source_id:
                raise InvalidLocatorAction("idempotency_key 已用于其他资产")
            if existing and str(existing[1]) == "registered":
                return {
                    "status": "registered",
                    "copy_request_id": int(existing[0]),
                    "source_id": int(existing[7] or source_id),
                    "locator_id": int(existing[3]),
                    "sync_request_id": int(existing[4]),
                }
            if existing and str(existing[1]) in {"prepared", "creating", "copying", "failed"}:
                raise InvalidLocatorAction("该副本请求正在处理中或结果不确定，请人工核对并恢复原请求，禁止重复创建")
            if existing:
                copy_request_id = int(existing[0])
                status = str(existing[1])
                new_api_doc_id = str(existing[2] or "")
                document_name = str(existing[6] or "")
                source_url = f"https://doc.weixin.qq.com/smartsheet/{new_api_doc_id}" if new_api_doc_id else ""
                source = _source_row(cur, source_id)
                env_profile = str(source[1])
                source_docid = str(source[2])
                if status != "external_created":
                    _require_copy_capability(cur, source_id)
            else:
                source = _source_row(cur, source_id)
                provider, env_profile, source_docid, source_type, document_name, status_value = source
                if provider != "wecom" or source_type == SYSTEM_SOURCE_TYPE or not valid_wecom_docid(str(source_docid)) or status_value != "active":
                    raise InvalidLocatorAction("该资产不支持创建副本")
                _require_copy_capability(cur, source_id)
                naming = now_name or (
                    lambda name: f"{name}-副本{datetime.now(tz=timezone.utc).strftime('%y%m%d_%H%M')}"
                )
                document_name = naming(str(document_name))
                cur.execute(
                    """
                    INSERT INTO document_copy_requests(idempotency_key, source_id, requested_by, requested_name, status)
                    VALUES (%s, %s, %s, %s, 'creating')
                    ON CONFLICT(idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    (key, source_id, requested_by, document_name),
                )
                inserted = cur.fetchone()
                if not inserted:
                    raise InvalidLocatorAction("该副本请求已由其他执行者接管，请稍后刷新")
                copy_request_id = int(inserted[0])
                status = "creating"
                new_api_doc_id = ""
                source_url = ""
                source_docid = str(source_docid)
        conn.commit()

    if status != "external_created":
        if copier is None:
            from app.integrations.wecom_docs import copy_smartsheet_doc

            copier = copy_smartsheet_doc
        created_persisted = False

        def persist_created(created_docid: str, created_url: str) -> None:
            nonlocal created_persisted
            if not valid_wecom_docid(str(created_docid or "")):
                raise InvalidLocatorAction("企微副本未返回有效 docid")
            with closing(connect()) as created_conn:
                with created_conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE document_copy_requests
                        SET status = 'copying', new_api_doc_id = %s, new_source_url = %s, updated_at = NOW()
                        WHERE id = %s AND status = 'creating'
                        """,
                        (created_docid, created_url, copy_request_id),
                    )
                created_conn.commit()
            created_persisted = True

        try:
            result = copier(
                env_profile=str(env_profile), source_docid=source_docid,
                new_doc_name=document_name, on_created=persist_created,
            )
        except Exception as exc:
            with closing(connect()) as failed_conn:
                with failed_conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE document_copy_requests
                        SET status = 'failed', error_kind = %s, error_summary = %s, updated_at = NOW()
                        WHERE id = %s AND new_api_doc_id IS NULL
                        """,
                        (type(exc).__name__, "企微副本创建失败", copy_request_id),
                    )
                failed_conn.commit()
            raise
        new_api_doc_id = str(result.get("new_docid") or "")
        source_url = str(result.get("url") or "")
        if not valid_wecom_docid(new_api_doc_id):
            raise InvalidLocatorAction("企微副本未返回有效 docid")
        if not created_persisted:
            persist_created(new_api_doc_id, source_url)
        with closing(connect()) as completed_conn:
            with completed_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE document_copy_requests
                    SET status = 'external_created', new_source_url = %s,
                        error_kind = '', error_summary = '', updated_at = NOW()
                    WHERE id = %s AND status = 'copying' AND new_api_doc_id = %s
                    """,
                    (source_url, copy_request_id, new_api_doc_id),
                )
            completed_conn.commit()
    with closing(connect()) as conn:
        return _register_external_copy(
            conn,
            copy_request_id=copy_request_id,
            env_profile=str(env_profile),
            api_doc_id=new_api_doc_id,
            source_url=source_url,
            document_name=document_name,
            requested_by=requested_by,
        )


def _default_client(profile: str) -> Any:
    from app.integrations.wecom_docs import WeComDocClient, credentials_for_profile

    corpid, secret = credentials_for_profile(profile)
    return WeComDocClient(corpid, secret)


def repair_docid(
    connect: Callable[[], Any],
    *,
    source_id: int,
    api_doc_id: str,
    requested_by: str,
    client_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    if not valid_wecom_docid(str(api_doc_id or "")):
        raise InvalidLocatorAction("企微 docid 格式无效")
    with closing(connect()) as conn:
        with conn.cursor() as cur:
            source = _source_row(cur, source_id)
    provider, env_profile, _old_id, source_type, old_name, status = source
    if provider != "wecom" or source_type != "smartsheet_link" or status != "active":
        raise InvalidLocatorAction("只能修复未解析的企微链接资产")
    client = (client_factory or _default_client)(str(env_profile))
    document_name = str(client.get_doc_name(api_doc_id) or old_name)
    sheets = client.get_sheets(api_doc_id)
    if not isinstance(sheets, list):
        raise InvalidLocatorAction("企微文档验证失败")

    with closing(connect()) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM external_sources
                    WHERE provider = 'wecom' AND env_profile = %s AND external_doc_id = %s
                      AND external_sheet_id = '' AND id <> %s
                    """,
                    (env_profile, api_doc_id, source_id),
                )
                duplicate = cur.fetchone()
                target_source_id = int(duplicate[0]) if duplicate else source_id
                if duplicate:
                    cur.execute(
                        "UPDATE external_sources SET status = 'disabled', updated_at = NOW() WHERE id = %s",
                        (source_id,),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE external_sources
                        SET external_doc_id = %s, source_type = 'registry_doc', document_name = %s,
                            source_name = %s, status = 'active', updated_at = NOW()
                        WHERE id = %s
                        """,
                        (api_doc_id, document_name, document_name, target_source_id),
                    )
                capabilities = Jsonb({"read": "verified", "write": "unknown", "copy": "allowed"})
                cur.execute(
                    """
                    SELECT id, locator_version FROM document_locator_registry
                    WHERE provider = 'wecom' AND env_profile = %s AND api_doc_id = %s
                    FOR UPDATE
                    """,
                    (env_profile, api_doc_id),
                )
                canonical = cur.fetchone()
                merged_locator: tuple[int, int] | None = None
                if canonical:
                    cur.execute(
                        """
                        UPDATE document_locator_registry
                        SET document_name = %s, source_kind = 'docid-repair', lifecycle_status = 'active',
                            syncability_status = 'verified', capabilities = %s, sheet_count = %s,
                            external_source_id = %s, locator_version = locator_version + 1,
                            last_verified_at = NOW(), last_error_code = '', last_error_summary = '', updated_at = NOW()
                        WHERE id = %s RETURNING id, locator_version
                        """,
                        (document_name, capabilities, len(sheets), target_source_id, int(canonical[0])),
                    )
                    locator_id, locator_version = map(int, cur.fetchone())
                    cur.execute(
                        """
                        UPDATE document_locator_registry
                        SET lifecycle_status = 'disabled', syncability_status = 'unverified',
                            capabilities = %s, external_source_id = NULL,
                            locator_version = locator_version + 1, last_error_code = 'merged',
                            last_error_summary = '已合并到已解析档案', updated_at = NOW()
                        WHERE external_source_id = %s AND api_doc_id IS NULL
                        RETURNING id, locator_version
                        """,
                        (Jsonb({"read": "unavailable", "write": "unavailable", "copy": "unavailable"}), source_id),
                    )
                    merged = cur.fetchone()
                    merged_locator = tuple(map(int, merged)) if merged else None
                else:
                    cur.execute(
                        """
                        UPDATE document_locator_registry
                        SET api_doc_id = %s, document_name = %s, source_kind = 'docid-repair',
                            lifecycle_status = 'active', syncability_status = 'verified', capabilities = %s,
                            sheet_count = %s, external_source_id = %s, locator_version = locator_version + 1,
                            last_verified_at = NOW(), last_error_code = '', last_error_summary = '', updated_at = NOW()
                        WHERE external_source_id = %s AND api_doc_id IS NULL
                        RETURNING id, locator_version
                        """,
                        (api_doc_id, document_name, capabilities, len(sheets), target_source_id, source_id),
                    )
                    repaired = cur.fetchone()
                    if repaired:
                        locator_id, locator_version = map(int, repaired)
                    else:
                        cur.execute(
                            """
                            INSERT INTO document_locator_registry(
                                provider, env_profile, api_doc_id, document_name, source_kind,
                                lifecycle_status, syncability_status, capabilities, sheet_count,
                                external_source_id, last_verified_at, updated_at
                            ) VALUES ('wecom', %s, %s, %s, 'docid-repair', 'active', 'verified', %s, %s, %s, NOW(), NOW())
                            RETURNING id, locator_version
                            """,
                            (env_profile, api_doc_id, document_name, capabilities, len(sheets), target_source_id),
                        )
                        locator_id, locator_version = map(int, cur.fetchone())
                cur.execute(
                    """
                    INSERT INTO document_locator_events(
                        locator_id, locator_version, event_type, trigger_source, changed_fields, status_summary, actor
                    ) VALUES (%s, %s, 'docid-repaired', 'sync-api', %s, %s, %s)
                    ON CONFLICT(locator_id, locator_version, event_type) DO NOTHING
                    """,
                    (locator_id, locator_version, Jsonb(["api_doc_id", "syncability_status"]), Jsonb({"status": "verified"}), requested_by),
                )
                cur.execute(
                    """
                    INSERT INTO document_locator_mirror_jobs(locator_id, locator_version, trigger, updated_at)
                    VALUES (%s, %s, 'docid-repaired', NOW())
                    ON CONFLICT(locator_id, locator_version) DO NOTHING
                    """,
                    (locator_id, locator_version),
                )
                if merged_locator:
                    cur.execute(
                        """
                        INSERT INTO document_locator_events(
                            locator_id, locator_version, event_type, trigger_source, changed_fields, status_summary, actor
                        ) VALUES (%s, %s, 'docid-merged', 'sync-api', %s, %s, %s)
                        ON CONFLICT(locator_id, locator_version, event_type) DO NOTHING
                        """,
                        (merged_locator[0], merged_locator[1], Jsonb(["lifecycle_status", "external_source_id"]), Jsonb({"status": "merged"}), requested_by),
                    )
                    cur.execute(
                        """
                        INSERT INTO document_locator_mirror_jobs(locator_id, locator_version, trigger, updated_at)
                        VALUES (%s, %s, 'docid-merged', NOW())
                        ON CONFLICT(locator_id, locator_version) DO NOTHING
                        """,
                        merged_locator,
                    )
                cur.execute(
                    """
                    INSERT INTO sync_requests(source_id, provider, env_profile, mode, status, requested_by)
                    VALUES (%s, 'wecom', %s, 'manual', 'pending', %s) RETURNING id
                    """,
                    (target_source_id, env_profile, requested_by),
                )
                sync_request_id = int(cur.fetchone()[0])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "status": "registered",
        "source_id": target_source_id,
        "locator_id": locator_id,
        "sync_request_id": sync_request_id,
    }
