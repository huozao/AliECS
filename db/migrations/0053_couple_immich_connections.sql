-- Each Couple user owns an Immich API credential. The value is application
-- encrypted (AES-GCM) and is never returned to the browser or stored in logs.
CREATE TABLE IF NOT EXISTS couple_immich_accounts (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    encrypted_api_key TEXT,
    immich_user_id TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_couple_immich_accounts_status
    ON couple_immich_accounts(status);

INSERT INTO schema_migrations(version)
VALUES ('0053_couple_immich_connections')
ON CONFLICT(version) DO NOTHING;
