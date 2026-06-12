-- 0013: session revocation column + missing indexes
ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version BIGINT NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_photos_memory_id ON photos(memory_id);
CREATE INDEX IF NOT EXISTS idx_couple_members_user_id ON couple_members(user_id);
