INSERT INTO permissions(code, name, description) VALUES
('quality_report.read', '质检报告查询', '查询和查看已发布质检报告'),
('quality_report.download', '质检报告下载', '下载已发布质检报告文件'),
('quality_report.manage', '质检报告维护', '创建报告草稿和上传文件'),
('quality_report.admin', '质检报告发布管理', '发布、替换和作废质检报告')
ON CONFLICT(code) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description;

INSERT INTO role_permissions(role_id, permission_id)
SELECT r.id, p.id
FROM roles r
JOIN permissions p ON p.code IN ('quality_report.read', 'quality_report.download')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions(role_id, permission_id)
SELECT r.id, p.id
FROM roles r
JOIN permissions p ON p.code = 'quality_report.manage'
WHERE r.code IN ('admin', 'tech_a', 'tech_b')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions(role_id, permission_id)
SELECT r.id, p.id
FROM roles r
JOIN permissions p ON p.code = 'quality_report.admin'
WHERE r.code = 'admin'
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS quality_storage_backends (
  id BIGSERIAL PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  provider TEXT NOT NULL CHECK (provider IN ('nutstore_webdav', 'oss', 'local_test')),
  display_name TEXT NOT NULL,
  credential_ref TEXT NOT NULL,
  base_path TEXT NOT NULL DEFAULT 'quality-reports',
  priority INTEGER NOT NULL DEFAULT 100,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'degraded', 'disabled')),
  monthly_upload_limit_bytes BIGINT NOT NULL DEFAULT 1073741824 CHECK (monthly_upload_limit_bytes > 0),
  uploaded_bytes_month BIGINT NOT NULL DEFAULT 0 CHECK (uploaded_bytes_month >= 0),
  upload_month DATE NOT NULL DEFAULT date_trunc('month', CURRENT_DATE)::date,
  last_health_check_at TIMESTAMPTZ,
  last_health_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO quality_storage_backends(
  code, provider, display_name, credential_ref, base_path, priority,
  monthly_upload_limit_bytes, status
) VALUES (
  'nutstore_qc_01', 'nutstore_webdav', '质检报告坚果云 01（polymerwang）',
  'QUALITY_WEBDAV_01', 'quality-reports', 100, 1073741824, 'active'
)
ON CONFLICT(code) DO UPDATE SET
  provider = EXCLUDED.provider,
  display_name = EXCLUDED.display_name,
  credential_ref = EXCLUDED.credential_ref,
  base_path = EXCLUDED.base_path,
  monthly_upload_limit_bytes = EXCLUDED.monthly_upload_limit_bytes,
  updated_at = NOW();

CREATE TABLE IF NOT EXISTS quality_reports (
  id BIGSERIAL PRIMARY KEY,
  report_no TEXT NOT NULL,
  product_code TEXT NOT NULL,
  product_name TEXT NOT NULL,
  batch_no TEXT,
  report_type TEXT NOT NULL,
  inspection_date DATE,
  issued_at DATE,
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'superseded', 'revoked')),
  recipe_snapshot_id TEXT,
  supersedes_report_id BIGINT REFERENCES quality_reports(id) ON DELETE SET NULL,
  created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  published_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  published_at TIMESTAMPTZ,
  UNIQUE(report_no, revision)
);

CREATE INDEX IF NOT EXISTS idx_quality_reports_product
  ON quality_reports(product_code, status, inspection_date DESC NULLS LAST, id DESC);
CREATE INDEX IF NOT EXISTS idx_quality_reports_batch
  ON quality_reports(batch_no) WHERE batch_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_quality_reports_no
  ON quality_reports(report_no, revision DESC);

CREATE TABLE IF NOT EXISTS quality_report_batches (
  report_id BIGINT NOT NULL REFERENCES quality_reports(id) ON DELETE CASCADE,
  batch_no TEXT NOT NULL,
  PRIMARY KEY(report_id, batch_no)
);

CREATE TABLE IF NOT EXISTS quality_report_recipe_links (
  report_id BIGINT NOT NULL REFERENCES quality_reports(id) ON DELETE CASCADE,
  recipe_snapshot_id TEXT NOT NULL,
  PRIMARY KEY(report_id, recipe_snapshot_id)
);

CREATE TABLE IF NOT EXISTS quality_report_files (
  id BIGSERIAL PRIMARY KEY,
  report_id BIGINT NOT NULL REFERENCES quality_reports(id) ON DELETE RESTRICT,
  storage_backend_id BIGINT NOT NULL REFERENCES quality_storage_backends(id) ON DELETE RESTRICT,
  remote_path TEXT NOT NULL,
  filename TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
  sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted', 'missing')),
  uploaded_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(report_id, sha256),
  UNIQUE(storage_backend_id, remote_path)
);

CREATE INDEX IF NOT EXISTS idx_quality_report_files_report
  ON quality_report_files(report_id, status, id);

INSERT INTO features(code, title, description, url, category, required_permission, status, sort_order)
VALUES (
  'quality_reports', '质检报告', '按产品、批次或报告编号查询质检报告',
  '/quality-reports/', '质检', 'quality_report.read', 'active', 85
)
ON CONFLICT(code) DO UPDATE SET
  title = EXCLUDED.title,
  description = EXCLUDED.description,
  url = EXCLUDED.url,
  category = EXCLUDED.category,
  required_permission = EXCLUDED.required_permission,
  status = EXCLUDED.status,
  sort_order = EXCLUDED.sort_order,
  updated_at = NOW();

UPDATE backup_policies
SET name = '质检报告文件存储',
    lifecycle_status = 'active',
    monitoring_required = TRUE,
    detail_json = '{"destinations":["质检报告坚果云 01（polymerwang）"],"storage_strategy":"每个文件只存一个账号；数据库记录账号、路径和 SHA-256；不进入核心 Restic 仓库","monthly_upload_limit_bytes":1073741824}'::jsonb,
    updated_at = NOW()
WHERE code = 'quality-reports';

COMMENT ON TABLE quality_storage_backends IS '质检文件存储池；只保存 credential_ref，绝不保存 WebDAV 密码';
COMMENT ON TABLE quality_report_files IS '质检报告文件定位元数据；每个文件只写入一个存储账号';
