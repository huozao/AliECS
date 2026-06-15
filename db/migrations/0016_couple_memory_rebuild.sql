-- Couple Memory rebuild finalization.
--
-- Adds missing columns and indexes needed by the in-app Couple Memory dashboard,
-- map, gallery, sharing, and Immich asset picker. No destructive changes.
-- Rollback, if needed: leave columns in place and disable feature flags
-- (COUPLE_FEATURE_ENABLED / IMMICH_ENABLED) before reverting application code.

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE memories
  ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE share_links
  ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;

ALTER TABLE couple_spaces
  ADD COLUMN IF NOT EXISTS start_date DATE,
  ADD COLUMN IF NOT EXISTS theme TEXT,
  ADD COLUMN IF NOT EXISTS cover_image_url TEXT;

ALTER TABLE couple_memory_assets
  ADD COLUMN IF NOT EXISTS thumbnail_url TEXT,
  ADD COLUMN IF NOT EXISTS exif JSONB,
  ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS lng DOUBLE PRECISION;

UPDATE couple_memory_assets
SET lat = latitude
WHERE lat IS NULL AND latitude IS NOT NULL;

UPDATE couple_memory_assets
SET lng = longitude
WHERE lng IS NULL AND longitude IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_photos_memory ON photos(memory_id);
CREATE INDEX IF NOT EXISTS idx_couple_members_user ON couple_members(user_id, couple_space_id);
CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag);
CREATE INDEX IF NOT EXISTS idx_memories_space_date ON memories(couple_space_id, memory_date);
CREATE INDEX IF NOT EXISTS idx_share_links_token ON share_links(token);

CREATE UNIQUE INDEX IF NOT EXISTS idx_couple_memory_assets_unique_asset
  ON couple_memory_assets(memory_id, provider, immich_asset_id)
  WHERE immich_asset_id IS NOT NULL;

INSERT INTO schema_migrations(version)
VALUES ('0016_couple_memory_rebuild')
ON CONFLICT(version) DO NOTHING;
