ALTER TABLE integration_events
    ADD COLUMN IF NOT EXISTS normalized_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS payload_hash TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_integration_events_payload_hash
    ON integration_events(provider, payload_hash)
    WHERE payload_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS integration_sync_requests (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    module TEXT NOT NULL,
    mode TEXT NOT NULL,
    target_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason_event_id TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'pending',
    dedupe_key TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    sync_run_id BIGINT,
    error_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_integration_sync_requests_status_priority
    ON integration_sync_requests(status, priority ASC, requested_at ASC);

CREATE INDEX IF NOT EXISTS idx_integration_sync_requests_provider_module
    ON integration_sync_requests(provider, module, requested_at DESC);

CREATE TABLE IF NOT EXISTS integration_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    module TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    row_count INTEGER NOT NULL DEFAULT 0,
    exit_code INTEGER,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_integration_sync_runs_provider_module_started
    ON integration_sync_runs(provider, module, started_at DESC);

CREATE TABLE IF NOT EXISTS integration_sync_snapshots (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    module TEXT NOT NULL,
    mode TEXT NOT NULL,
    sync_run_id BIGINT REFERENCES integration_sync_runs(id) ON DELETE SET NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    snapshot_hash TEXT NOT NULL,
    source_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_integration_sync_snapshots_provider_module
    ON integration_sync_snapshots(provider, module, created_at DESC);

CREATE TABLE IF NOT EXISTS integration_reconciliation_diffs (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    module TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'needs_review',
    severity TEXT NOT NULL DEFAULT 'warning',
    summary TEXT NOT NULL,
    diff_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    full_snapshot_id BIGINT REFERENCES integration_sync_snapshots(id) ON DELETE SET NULL,
    incremental_snapshot_id BIGINT REFERENCES integration_sync_snapshots(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_integration_reconciliation_diffs_status
    ON integration_reconciliation_diffs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS tplus_bom_records (
    record_key TEXT PRIMARY KEY,
    record_hash TEXT NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_sync_run_id BIGINT REFERENCES integration_sync_runs(id) ON DELETE SET NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    missing_since TIMESTAMPTZ
);
