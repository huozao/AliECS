-- 资料任务外部归档：Paperless 文档回填 + ERPNext 建档关联。
-- 幂等：全部 ADD COLUMN IF NOT EXISTS，可重复执行。

-- 每个原件在 Paperless 里对应一个文档；回填文档 ID、可访问链接、上传任务 uuid 和最近错误。
ALTER TABLE wecom_kf_material_items
    ADD COLUMN IF NOT EXISTS paperless_task_uuid TEXT NOT NULL DEFAULT '';
ALTER TABLE wecom_kf_material_items
    ADD COLUMN IF NOT EXISTS paperless_document_id BIGINT;
ALTER TABLE wecom_kf_material_items
    ADD COLUMN IF NOT EXISTS paperless_document_url TEXT NOT NULL DEFAULT '';
ALTER TABLE wecom_kf_material_items
    ADD COLUMN IF NOT EXISTS paperless_error TEXT NOT NULL DEFAULT '';

-- 任务级：外部归档整体状态、ERPNext 记录引用、最近错误。
-- external_archive_status: none=未启用/未开始 pending=进行中 completed=全部成功 partial=部分失败 failed=失败
ALTER TABLE wecom_kf_material_tasks
    ADD COLUMN IF NOT EXISTS external_archive_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE wecom_kf_material_tasks
    ADD COLUMN IF NOT EXISTS external_archive_error TEXT NOT NULL DEFAULT '';
ALTER TABLE wecom_kf_material_tasks
    ADD COLUMN IF NOT EXISTS erpnext_doctype TEXT NOT NULL DEFAULT '';
ALTER TABLE wecom_kf_material_tasks
    ADD COLUMN IF NOT EXISTS erpnext_docname TEXT NOT NULL DEFAULT '';
ALTER TABLE wecom_kf_material_tasks
    ADD COLUMN IF NOT EXISTS erpnext_url TEXT NOT NULL DEFAULT '';

-- CHECK 约束单独加（IF NOT EXISTS 语义用 DO 块保证可重复执行）。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_wecom_kf_external_archive_status'
    ) THEN
        ALTER TABLE wecom_kf_material_tasks
            ADD CONSTRAINT ck_wecom_kf_external_archive_status
            CHECK (external_archive_status IN ('none', 'pending', 'completed', 'partial', 'failed'));
    END IF;
END$$;

-- 便于运维查询"本地已完成但外部归档未成功"的任务。
CREATE INDEX IF NOT EXISTS ix_wecom_kf_tasks_external_archive
    ON wecom_kf_material_tasks(external_archive_status)
    WHERE external_archive_status IN ('pending', 'partial', 'failed');
