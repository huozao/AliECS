CREATE TABLE IF NOT EXISTS image_backfill_log (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    env_profile TEXT NOT NULL,
    external_doc_id TEXT NOT NULL,
    sheet_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    sp_no TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    image_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_image_backfill_log_status CHECK (status IN ('done', 'no_image', 'error'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_image_backfill_log_record
    ON image_backfill_log(external_doc_id, sheet_id, record_id);

CREATE INDEX IF NOT EXISTS idx_image_backfill_log_status
    ON image_backfill_log(status, updated_at);
