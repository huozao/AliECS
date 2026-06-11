-- doc 级登记行记录文档 modify_time，全量同步时未变化的文档整体跳过（节省企微 API 调用）。
ALTER TABLE external_sources ADD COLUMN IF NOT EXISTS external_modified_at TEXT;
