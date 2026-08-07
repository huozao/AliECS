-- 建议编码记忆：记录已被确认占用/推荐过的存货编码，避免再次推荐重复父件编码。
CREATE TABLE IF NOT EXISTS tplus_inventory_code_memory (
    code TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'live_duplicate',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
