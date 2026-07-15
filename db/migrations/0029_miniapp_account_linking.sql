CREATE TABLE IF NOT EXISTS miniapp_account_requests (
    id BIGSERIAL PRIMARY KEY,
    openid TEXT NOT NULL,
    request_type TEXT NOT NULL CHECK (request_type IN ('bind_existing', 'create_new')),
    requested_username TEXT NOT NULL,
    display_name TEXT NOT NULL,
    department TEXT,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    target_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    review_note TEXT,
    reviewed_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_miniapp_account_requests_pending_openid
    ON miniapp_account_requests(openid)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_miniapp_account_requests_status_created
    ON miniapp_account_requests(status, created_at DESC);

CREATE TABLE IF NOT EXISTS miniapp_account_links (
    id BIGSERIAL PRIMARY KEY,
    openid TEXT NOT NULL UNIQUE,
    user_id BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    request_id BIGINT REFERENCES miniapp_account_requests(id) ON DELETE SET NULL,
    bound_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE miniapp_account_requests IS '微信小程序用户申请绑定已有 SSO 账号或申请新建 SSO 账号';
COMMENT ON TABLE miniapp_account_links IS '微信 OpenID 与既有 users/SSO 账号的一对一绑定，不存储 SSO 密码';
