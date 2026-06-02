CREATE TABLE IF NOT EXISTS integration_events (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    event_type TEXT,
    event_id TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_integration_events_provider_event_id
    ON integration_events(provider, event_id)
    WHERE event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_integration_events_provider_received
    ON integration_events(provider, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_integration_events_status_received
    ON integration_events(status, received_at ASC);

CREATE TABLE IF NOT EXISTS integration_tokens (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    token_type TEXT NOT NULL,
    env_profile TEXT NOT NULL DEFAULT 'default',
    token_value_encrypted TEXT,
    expires_at TIMESTAMPTZ,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider, token_type, env_profile)
);

CREATE INDEX IF NOT EXISTS idx_integration_tokens_provider_type
    ON integration_tokens(provider, token_type, env_profile);
