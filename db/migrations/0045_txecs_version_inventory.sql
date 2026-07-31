-- Register txecs in the existing version/stale channel and recognize TCR mirrors.
INSERT INTO backup_policies
    (code, name, purpose, asset, source_device, method, schedule_label,
     expected_interval_seconds, warning_after_seconds, failure_after_seconds,
     retention_policy, lifecycle_status, monitoring_required, sort_order, detail_json)
VALUES
    ('version-inventory-txecs', '版本采集心跳（txecs）', '确认 txecs 每日版本与漂移采集脚本在跑',
     'txecs 源码/配置/容器/apt 只读快照', 'txecs', 'systemd timer 每日采集上报', '每日 05:05',
     86400, 172800, 259200, '仅保留最新快照', 'active', TRUE, 100, '{}'::jsonb)
ON CONFLICT (code) DO UPDATE SET
    name=EXCLUDED.name, purpose=EXCLUDED.purpose, asset=EXCLUDED.asset,
    source_device=EXCLUDED.source_device, method=EXCLUDED.method,
    schedule_label=EXCLUDED.schedule_label,
    expected_interval_seconds=EXCLUDED.expected_interval_seconds,
    warning_after_seconds=EXCLUDED.warning_after_seconds,
    failure_after_seconds=EXCLUDED.failure_after_seconds,
    retention_policy=EXCLUDED.retention_policy,
    lifecycle_status=EXCLUDED.lifecycle_status,
    monitoring_required=EXCLUDED.monitoring_required,
    sort_order=EXCLUDED.sort_order, detail_json=EXCLUDED.detail_json, updated_at=NOW();

INSERT INTO version_components
    (component_key, display_name, kind, match_images, devices, upstream_source,
     upstream_ref, version_pattern, pin_note, family, sort_order)
VALUES
    ('postgres-txecs', 'PostgreSQL（txecs 生产）', 'docker-image', '{postgres}', '{txecs}',
     'dockerhub', 'library/postgres', '^16\.', '锁 16 大版本，只比对 16.x 内小版本升级',
     'third-party', 30)
ON CONFLICT (component_key) DO UPDATE SET
    display_name=EXCLUDED.display_name, kind=EXCLUDED.kind,
    match_images=EXCLUDED.match_images, devices=EXCLUDED.devices,
    upstream_source=EXCLUDED.upstream_source, upstream_ref=EXCLUDED.upstream_ref,
    version_pattern=EXCLUDED.version_pattern, pin_note=EXCLUDED.pin_note,
    family=EXCLUDED.family, sort_order=EXCLUDED.sort_order, updated_at=NOW();

UPDATE version_components
SET devices = '{txecs}', updated_at = NOW()
WHERE component_key IN ('openclaw', 'authelia', 'lldap');

UPDATE version_components
SET devices = '{aliecs,txecs}',
    match_images = ARRAY[
        'ghcr.io/huozao/backend-api', 'ghcr.io/huozao/public-web', 'ghcr.io/huozao/admin-ui',
        'ccr.ccs.tencentyun.com/hydwang-infra/backend-api',
        'ccr.ccs.tencentyun.com/hydwang-infra/public-web',
        'ccr.ccs.tencentyun.com/hydwang-infra/admin-ui',
        'ccr.ccs.tencentyun.com/hydwang-infra/doc-sync-worker',
        'ccr.ccs.tencentyun.com/hydwang-infra/tplus-sync-worker'
    ],
    pin_note = '自家镜像；运行版本以 Git SHA、workflow run 和 OCI digest 为准',
    updated_at = NOW()
WHERE component_key = 'aliecs-services';

UPDATE version_components
SET devices = '{aliecs,txecs}',
    match_images = ARRAY[
        'ghcr.io/huozao/openclaw-bridge',
        'ccr.ccs.tencentyun.com/hydwang-infra/openclaw-bridge'
    ],
    pin_note = 'bridge 内容变化时自动 cutover；回滚和重切走 workflow_dispatch',
    updated_at = NOW()
WHERE component_key = 'openclaw-bridge';
