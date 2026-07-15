CREATE TABLE IF NOT EXISTS backup_policies (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    asset TEXT NOT NULL,
    source_device TEXT NOT NULL,
    method TEXT NOT NULL,
    schedule_label TEXT NOT NULL,
    expected_interval_seconds INTEGER,
    warning_after_seconds INTEGER,
    failure_after_seconds INTEGER,
    retention_policy TEXT NOT NULL DEFAULT '',
    lifecycle_status TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_status IN ('active', 'planned', 'covered', 'passive', 'excluded')),
    monitoring_required BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 100,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS backup_runs (
    id BIGSERIAL PRIMARY KEY,
    policy_code TEXT NOT NULL REFERENCES backup_policies(code) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
    source_device TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    snapshot_id TEXT,
    data_bytes BIGINT,
    file_count BIGINT,
    destinations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (policy_code, run_id)
);

CREATE INDEX IF NOT EXISTS idx_backup_runs_policy_finished
    ON backup_runs(policy_code, finished_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS backup_restore_checks (
    id BIGSERIAL PRIMARY KEY,
    policy_code TEXT NOT NULL REFERENCES backup_policies(code) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_snapshot_id TEXT,
    target_device TEXT,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO backup_policies(
    code, name, purpose, asset, source_device, method, schedule_label,
    expected_interval_seconds, warning_after_seconds, failure_after_seconds,
    retention_policy, lifecycle_status, monitoring_required, sort_order, detail_json
)
VALUES
    ('core-restic', '核心业务 Restic', '恢复核心业务数据、统一账号、审计和 OpenClaw 状态',
     'PostgreSQL 全库；OpenClaw 状态与认证', 'aliecs', 'Restic + WebDAV', '每日 03:30',
     86400, 108000, 172800, '每日 7 / 每周 4 / 每月 3', 'active', TRUE, 10,
     '{"destinations":["坚果云主库（polymerwang）","坚果云副库（polymerone）"]}'::jsonb),
    ('quality-reports', '质检报告文件', '保护已发布质检报告及其不可变修订版本',
     '质检报告 PDF 和源文件', 'aliecs', 'WebDAV 主副本 + SHA-256', '上传后立即复制',
     NULL, NULL, NULL, '发布版本长期保留', 'planned', FALSE, 20,
     '{"destinations":["坚果云主库（polymerwang）","坚果云副库（polymerone）"]}'::jsonb),
    ('tplus-raw', 'T+ 原始数据归档', '保留 T+ 原始 JSON 以便复查和重放',
     'T+ 原始 JSON', 'aliecs → webdock2', 'age 加密归档', '每日',
     86400, 108000, 172800, '第 15–90 天', 'planned', FALSE, 30,
     '{"destination":"webdock2 D:\\\\WebDockArchive\\\\aliecs\\\\tplus-raw","note":"脚本存在，运行态待纳管"}'::jsonb),
    ('wecom-structure', '企微结构快照', '追溯企业、部门、成员和表结构变化',
     '企微结构备份表及 PostgreSQL 记录', 'aliecs', '业务快照；随 PostgreSQL 再备份', '按同步产生',
     NULL, NULL, NULL, '随 PostgreSQL 保留策略', 'covered', FALSE, 40,
     '{"covered_by":"core-restic"}'::jsonb),
    ('source-config', '源码与加密配置', '重建应用、主机配置和部署脚本',
     'Git 仓库；SOPS 密文', 'devbox / GitHub', 'Git + SOPS/age', '每次提交',
     NULL, NULL, NULL, 'Git 历史', 'passive', FALSE, 50, '{}'::jsonb),
    ('webdock-browser-data', 'WebDock 浏览器登录态', 'ChatGPT 浏览器登录态',
     'browser_data', 'webdock1 / webdock2', '当前不备份', '不适用',
     NULL, NULL, NULL, '明确排除；需要人工重新登录', 'excluded', FALSE, 60, '{}'::jsonb),
    ('webdock-system-image', 'Windows / WSL 整机镜像', '整机灾难恢复',
     'Windows 与 WSL 虚拟磁盘', 'webdock2', '当前不做整机备份', '不适用',
     NULL, NULL, NULL, '通过 Git 和部署脚本重建', 'excluded', FALSE, 70, '{}'::jsonb)
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

COMMENT ON TABLE backup_policies IS '项目备份保护范围、目的、周期和保留策略总账';
COMMENT ON TABLE backup_runs IS '备份任务执行结果及主副存储状态';
COMMENT ON TABLE backup_restore_checks IS '完整性检查与恢复演练结果';
