CREATE TABLE IF NOT EXISTS document_locator_registry (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    env_profile TEXT NOT NULL,
    api_doc_id TEXT,
    share_ref TEXT,
    document_name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    admin_userids JSONB NOT NULL DEFAULT '[]'::jsonb,
    credential_ref TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL,
    syncability_status TEXT NOT NULL,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    sheet_count INTEGER NOT NULL DEFAULT 0 CHECK (sheet_count >= 0),
    external_source_id BIGINT REFERENCES external_sources(id) ON DELETE SET NULL,
    locator_version INTEGER NOT NULL DEFAULT 1 CHECK (locator_version > 0),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_verified_at TIMESTAMPTZ,
    last_sync_at TIMESTAMPTZ,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_summary TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (lifecycle_status IN ('active', 'disabled', 'unresolved')),
    CHECK (syncability_status IN ('verified', 'unverified', 'invalid-id', 'permission-denied')),
    CHECK (api_doc_id IS NOT NULL OR share_ref IS NOT NULL),
    CHECK (api_doc_id IS NULL OR api_doc_id <> ''),
    CHECK (share_ref IS NULL OR share_ref <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_locator_registry_api_identity
    ON document_locator_registry(provider, env_profile, api_doc_id)
    WHERE api_doc_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_locator_registry_share_identity
    ON document_locator_registry(provider, env_profile, share_ref)
    WHERE share_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_document_locator_registry_source
    ON document_locator_registry(external_source_id);

CREATE TABLE IF NOT EXISTS document_locator_events (
    id BIGSERIAL PRIMARY KEY,
    locator_id BIGINT NOT NULL REFERENCES document_locator_registry(id) ON DELETE CASCADE,
    locator_version INTEGER NOT NULL CHECK (locator_version > 0),
    event_type TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    changed_fields JSONB NOT NULL DEFAULT '[]'::jsonb,
    status_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    actor TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_locator_events_version_type
    ON document_locator_events(locator_id, locator_version, event_type);

CREATE INDEX IF NOT EXISTS idx_document_locator_events_created
    ON document_locator_events(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS document_locator_mirror_jobs (
    id BIGSERIAL PRIMARY KEY,
    locator_id BIGINT NOT NULL REFERENCES document_locator_registry(id) ON DELETE CASCADE,
    locator_version INTEGER NOT NULL CHECK (locator_version > 0),
    trigger TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'success', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_locator_mirror_jobs_version
    ON document_locator_mirror_jobs(locator_id, locator_version);

CREATE INDEX IF NOT EXISTS idx_document_locator_mirror_jobs_pending
    ON document_locator_mirror_jobs(status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS document_copy_requests (
    id BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    source_id BIGINT NOT NULL REFERENCES external_sources(id) ON DELETE RESTRICT,
    requested_by TEXT NOT NULL DEFAULT '',
    requested_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'prepared'
        CHECK (status IN ('prepared', 'external_created', 'registered', 'failed')),
    new_api_doc_id TEXT,
    new_source_url TEXT NOT NULL DEFAULT '',
    locator_id BIGINT REFERENCES document_locator_registry(id) ON DELETE SET NULL,
    sync_request_id BIGINT REFERENCES sync_requests(id) ON DELETE SET NULL,
    error_kind TEXT NOT NULL DEFAULT '',
    error_summary TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_document_copy_requests_status
    ON document_copy_requests(status, updated_at, id);
