-- 0017: T+ 定时同步开关与间隔配置（单行，worker 与后端共用）
-- 初始 enabled=true / interval_seconds=86400 → 不改变现有行为。
CREATE TABLE IF NOT EXISTS integration_sync_config (
    provider         text PRIMARY KEY,
    enabled          boolean NOT NULL DEFAULT true,
    interval_seconds integer NOT NULL DEFAULT 86400,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       text
);

INSERT INTO integration_sync_config(provider) VALUES ('chanjet')
ON CONFLICT (provider) DO NOTHING;
