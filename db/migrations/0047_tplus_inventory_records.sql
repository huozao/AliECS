-- T+ 存货主数据（存货档案）镜像表：父件核对与 /formula/colors 匹配时，
-- 除了 tplus_bom_records（只有 BOM 记录），还要认「仅建存货、暂无 BOM」的父件，
-- 否则新建的纯存货父件会被误判「编码失联」。
CREATE TABLE IF NOT EXISTS tplus_inventory_records (
    id BIGSERIAL PRIMARY KEY,
    record_key TEXT NOT NULL UNIQUE,
    record_hash TEXT NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    missing_since TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tplus_inventory_records_active
    ON tplus_inventory_records(missing_since, last_seen_at DESC);
