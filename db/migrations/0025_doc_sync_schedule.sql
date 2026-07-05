-- 0025: 文档同步（企微+飞书）定时调度配置：起点时间 + 表格拉取暂停开关。
-- anchor_time：北京时间 HH:MM，空串=不锚定（沿用"上次+interval"）。
-- pull_paused：管理页应急覆盖时暂停飞书「配置表」拉取，防止手动值被表格覆盖。
ALTER TABLE integration_sync_config ADD COLUMN IF NOT EXISTS anchor_time text NOT NULL DEFAULT '';
ALTER TABLE integration_sync_config ADD COLUMN IF NOT EXISTS pull_paused boolean NOT NULL DEFAULT false;

INSERT INTO integration_sync_config(provider) VALUES ('doc_sync')
ON CONFLICT (provider) DO NOTHING;
