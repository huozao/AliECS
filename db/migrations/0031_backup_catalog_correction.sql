UPDATE backup_policies
SET name = '核心系统 Restic（单仓库）',
    purpose = '恢复 PostgreSQL 核心业务数据和 OpenClaw 状态',
    asset = 'PostgreSQL 全库；OpenClaw 状态与认证',
    source_device = 'aliecs',
    method = 'Restic 加密 + max 压缩 + 块级去重',
    schedule_label = '每日 03:30',
    retention_policy = '每日 7 / 每周 4 / 每月 3；每月清理未引用数据',
    lifecycle_status = 'active',
    monitoring_required = TRUE,
    detail_json = '{"destinations":["核心 Restic（polymerone）"],"scope":"仅 PostgreSQL 与 OpenClaw；不含质检报告文件、browser_data 或整机数据","replication":"不保存重复副本"}'::jsonb,
    updated_at = NOW()
WHERE code = 'core-restic';

UPDATE backup_policies
SET name = '质检报告文件存储（待建设）',
    purpose = '保存已发布质检报告及其不可变修订版本',
    asset = '质检报告 PDF 和管理员可见源文件',
    source_device = 'aliecs 元数据 / 坚果云文件层',
    method = 'WebDAV 存储池；单文件单副本；按账号月上传额度分配',
    schedule_label = '随报告上传写入',
    retention_policy = '发布版本长期保留；替换和作废保留历史',
    lifecycle_status = 'planned',
    monitoring_required = FALSE,
    detail_json = '{"destinations":[],"status_note":"尚未实施，不代表文件已经备份","storage_strategy":"每个文件只存一个账号；数据库记录账号、路径和 SHA-256；不进入核心 Restic 仓库"}'::jsonb,
    updated_at = NOW()
WHERE code = 'quality-reports';

COMMENT ON TABLE backup_restore_checks IS '真实完整性检查或恢复演练结果；仅建表不代表校验任务已经实施';
