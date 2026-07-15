CREATE TABLE IF NOT EXISTS business_audit_events (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    actor_username_snapshot TEXT NOT NULL DEFAULT '',
    auth_source TEXT NOT NULL DEFAULT 'unknown',
    client_channel TEXT NOT NULL DEFAULT 'unknown'
        CHECK (client_channel IN ('website', 'miniapp', 'admin', 'machine', 'unknown')),
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    resource_revision TEXT,
    query_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_count INTEGER,
    outcome TEXT NOT NULL DEFAULT 'success'
        CHECK (outcome IN ('success', 'failed')),
    error_code TEXT,
    request_id TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    file_sha256 TEXT CHECK (file_sha256 IS NULL OR file_sha256 ~ '^[0-9a-f]{64}$'),
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_business_audit_actor_created
    ON business_audit_events(actor_user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_business_audit_action_created
    ON business_audit_events(action, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_business_audit_resource_created
    ON business_audit_events(resource_type, resource_id, created_at DESC, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_business_audit_request_action_resource
    ON business_audit_events(request_id, action, COALESCE(resource_id, ''));

COMMENT ON TABLE business_audit_events IS '人工使用关键业务操作审计；不得写入密码、令牌、坚果云凭据或完整敏感业务内容';
