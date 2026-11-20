-- Migration 0065: tplus_bom_history
-- Description: 物料清单 (BOM) 增量同步与多版本历史归档体系：建立全量历史演化表，扩展当前生效表主键关联

CREATE TABLE IF NOT EXISTS tplus_bom_history (
    id BIGSERIAL PRIMARY KEY,
    bom_code TEXT NOT NULL,
    bom_name TEXT NOT NULL DEFAULT '',
    bom_version TEXT NOT NULL,
    tplus_bom_id BIGINT,
    revision INTEGER NOT NULL DEFAULT 1,
    business_hash TEXT NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    change_type TEXT NOT NULL DEFAULT 'INITIAL',
    diff_summary JSONB DEFAULT '[]'::jsonb,
    notes TEXT DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    source_sync_run_id BIGINT REFERENCES integration_sync_runs(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tplus_bom_history_code_ver ON tplus_bom_history (bom_code, bom_version);
CREATE INDEX IF NOT EXISTS idx_tplus_bom_history_active ON tplus_bom_history (bom_code, is_active);
CREATE INDEX IF NOT EXISTS idx_tplus_bom_history_tplus_id ON tplus_bom_history (tplus_bom_id);

ALTER TABLE tplus_bom_records 
ADD COLUMN IF NOT EXISTS business_hash TEXT,
ADD COLUMN IF NOT EXISTS current_revision INTEGER DEFAULT 1,
ADD COLUMN IF NOT EXISTS history_id BIGINT REFERENCES tplus_bom_history(id) ON DELETE SET NULL;
