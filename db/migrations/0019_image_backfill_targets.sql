CREATE TABLE IF NOT EXISTS image_backfill_targets (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    env_profile TEXT NOT NULL,
    external_doc_id TEXT NOT NULL,
    sheet_title TEXT NOT NULL,
    attachment_field_title TEXT NOT NULL,
    image_field_title TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_image_backfill_targets_identity
    ON image_backfill_targets(provider, env_profile, external_doc_id, sheet_title, attachment_field_title, image_field_title);

-- Runtime targets are registered from private environment/registry data.
-- Public migrations intentionally contain no WeCom document identifiers.
