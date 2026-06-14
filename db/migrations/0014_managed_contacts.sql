CREATE TABLE IF NOT EXISTS managed_contacts (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    display_name TEXT,
    remark TEXT,
    enabled BOOLEAN NOT NULL DEFAULT true,
    project_url TEXT,
    project_name TEXT,
    tags TEXT,
    daily_quota INTEGER,
    notes TEXT,
    source_sheet TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(channel, peer_id)
);

CREATE INDEX IF NOT EXISTS idx_managed_contacts_channel_enabled
    ON managed_contacts(channel, enabled);
