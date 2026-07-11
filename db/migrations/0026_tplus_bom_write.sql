INSERT INTO permissions(code, name, description) VALUES
('tplus.bom.write', 'T+ BOM 写入', '创建并提交 T+ 物料清单')
ON CONFLICT (code) DO NOTHING;

WITH admin_role AS (SELECT id FROM roles WHERE code = 'admin')
INSERT INTO role_permissions(role_id, permission_id)
SELECT admin_role.id, p.id
FROM admin_role, permissions p
WHERE p.code = 'tplus.bom.write'
ON CONFLICT DO NOTHING;

INSERT INTO features(code, title, description, url, category, required_permission, status, sort_order)
VALUES ('tplus_bom_builder', '新建 T+ BOM', '选择已有父件和子件并提交物料清单', '/bom-builder/', '业务录入', 'tplus.bom.write', 'active', 35)
ON CONFLICT (code) DO UPDATE SET
    title = EXCLUDED.title,
    description = EXCLUDED.description,
    url = EXCLUDED.url,
    category = EXCLUDED.category,
    required_permission = EXCLUDED.required_permission,
    status = EXCLUDED.status,
    sort_order = EXCLUDED.sort_order,
    updated_at = NOW();

CREATE TABLE IF NOT EXISTS tplus_bom_drafts (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'draft',
    parent_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    children_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    options_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at TIMESTAMPTZ,
    CONSTRAINT ck_tplus_bom_drafts_status CHECK (status IN ('draft', 'submitted'))
);

CREATE INDEX IF NOT EXISTS idx_tplus_bom_drafts_owner_updated
    ON tplus_bom_drafts(created_by, updated_at DESC);

CREATE TABLE IF NOT EXISTS tplus_bom_submissions (
    id BIGSERIAL PRIMARY KEY,
    draft_id BIGINT NOT NULL UNIQUE REFERENCES tplus_bom_drafts(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending',
    idempotency_key TEXT NOT NULL UNIQUE,
    requested_by TEXT NOT NULL,
    request_json JSONB NOT NULL,
    response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    verification_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_bom_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_tplus_bom_submissions_status
        CHECK (status IN ('pending', 'processing', 'success', 'failed', 'needs_review'))
);

CREATE INDEX IF NOT EXISTS idx_tplus_bom_submissions_status_requested
    ON tplus_bom_submissions(status, requested_at ASC);

CREATE TABLE IF NOT EXISTS tplus_bom_submission_events (
    id BIGSERIAL PRIMARY KEY,
    submission_id BIGINT NOT NULL REFERENCES tplus_bom_submissions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tplus_bom_submission_events_submission
    ON tplus_bom_submission_events(submission_id, created_at ASC, id ASC);
