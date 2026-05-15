CREATE TABLE IF NOT EXISTS external_sources (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    env_profile TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    external_doc_id TEXT NOT NULL,
    external_sheet_id TEXT NOT NULL,
    source_url TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_sync_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider, env_profile, external_doc_id, external_sheet_id)
);

CREATE TABLE IF NOT EXISTS external_fields (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
    external_field_id TEXT NOT NULL,
    field_title TEXT NOT NULL,
    field_type TEXT,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, external_field_id)
);

CREATE TABLE IF NOT EXISTS external_records (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
    external_record_id TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    normalized_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    external_created_at TEXT,
    external_updated_at TEXT,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, external_record_id)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    env_profile TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    source_count INTEGER NOT NULL DEFAULT 0,
    sheet_count INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    error_json JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS sync_requests (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'wecom',
    env_profile TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    sync_run_id BIGINT REFERENCES sync_runs(id) ON DELETE SET NULL,
    error_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_external_sources_provider_profile
    ON external_sources(provider, env_profile);

CREATE INDEX IF NOT EXISTS idx_external_records_source_synced
    ON external_records(source_id, synced_at DESC);

CREATE INDEX IF NOT EXISTS idx_sync_runs_provider_profile_started
    ON sync_runs(provider, env_profile, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_sync_requests_status_requested
    ON sync_requests(status, requested_at ASC);
