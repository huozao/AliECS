-- 新增「Docker 镜像清理与盘点」备份策略，复用现有备份看板管道（backup_policies / backup_runs）。
-- 运行脚本见 infra/backup/docker-image-maintenance.sh：每周清理 30 天前未使用镜像并上报数量/容量。
INSERT INTO backup_policies(
    code, name, purpose, asset, source_device, method, schedule_label,
    expected_interval_seconds, warning_after_seconds, failure_after_seconds,
    retention_policy, lifecycle_status, monitoring_required, sort_order, detail_json
)
VALUES
    ('docker-image-prune', 'Docker 镜像清理与盘点',
     '定期清理未使用的 Docker 镜像并记录镜像数量与占用容量，防止磁盘被历史构建镜像占满',
     'aliecs 本机 Docker 镜像库（/var/lib/docker）', 'aliecs', 'docker image prune + 盘点上报',
     '每周日 04:00', 604800, 864000, 1296000,
     '删除 30 天前未使用镜像；在用与近 30 天镜像保留', 'active', TRUE, 80,
     '{"store":"aliecs:/var/lib/docker","fields":{"file_count":"镜像数量","data_bytes":"镜像总占用字节"}}'::jsonb)
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    purpose = EXCLUDED.purpose,
    asset = EXCLUDED.asset,
    source_device = EXCLUDED.source_device,
    method = EXCLUDED.method,
    schedule_label = EXCLUDED.schedule_label,
    expected_interval_seconds = EXCLUDED.expected_interval_seconds,
    warning_after_seconds = EXCLUDED.warning_after_seconds,
    failure_after_seconds = EXCLUDED.failure_after_seconds,
    retention_policy = EXCLUDED.retention_policy,
    lifecycle_status = EXCLUDED.lifecycle_status,
    monitoring_required = EXCLUDED.monitoring_required,
    sort_order = EXCLUDED.sort_order,
    detail_json = EXCLUDED.detail_json,
    updated_at = NOW();
