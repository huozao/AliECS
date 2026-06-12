CREATE TABLE IF NOT EXISTS couple_memory_assets (
    id BIGSERIAL PRIMARY KEY,
    couple_space_id BIGINT NOT NULL REFERENCES couple_spaces(id) ON DELETE CASCADE,
    memory_id BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'immich',
    immich_asset_id TEXT,
    immich_album_id TEXT,
    original_filename TEXT,
    taken_at TIMESTAMPTZ,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    thumbnail_cache_key TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    selected_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT couple_memory_assets_provider_check CHECK (provider IN ('immich')),
    CONSTRAINT couple_memory_assets_asset_or_album_check CHECK (
        immich_asset_id IS NOT NULL OR immich_album_id IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_couple_memory_assets_memory_order
    ON couple_memory_assets(memory_id, sort_order, id);

CREATE INDEX IF NOT EXISTS idx_couple_memory_assets_provider_asset
    ON couple_memory_assets(provider, immich_asset_id);
