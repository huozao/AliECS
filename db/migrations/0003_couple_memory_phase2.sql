-- Phase 2: Couple Memory 核心数据模型

CREATE TABLE IF NOT EXISTS couple_spaces (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  start_date DATE,
  theme TEXT,
  cover_image_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS couple_members (
  id BIGSERIAL PRIMARY KEY,
  couple_space_id BIGINT NOT NULL REFERENCES couple_spaces(id) ON DELETE CASCADE,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'member',
  joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(couple_space_id, user_id)
);

CREATE TABLE IF NOT EXISTS memories (
  id BIGSERIAL PRIMARY KEY,
  couple_space_id BIGINT NOT NULL REFERENCES couple_spaces(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  content TEXT,
  memory_date DATE,
  place_name TEXT,
  latitude DOUBLE PRECISION,
  longitude DOUBLE PRECISION,
  cover_photo_url TEXT,
  visibility TEXT NOT NULL DEFAULT 'private',
  created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (visibility IN ('private', 'shareable'))
);

CREATE TABLE IF NOT EXISTS photos (
  id BIGSERIAL PRIMARY KEY,
  couple_space_id BIGINT NOT NULL REFERENCES couple_spaces(id) ON DELETE CASCADE,
  memory_id BIGINT REFERENCES memories(id) ON DELETE SET NULL,
  original_filename TEXT,
  original_storage_url TEXT,
  thumbnail_url TEXT,
  display_url TEXT,
  taken_at TIMESTAMPTZ,
  latitude DOUBLE PRECISION,
  longitude DOUBLE PRECISION,
  exif_json JSONB,
  storage_driver TEXT NOT NULL DEFAULT 'local',
  external_library_type TEXT,
  external_asset_id TEXT,
  external_original_path TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS anniversaries (
  id BIGSERIAL PRIMARY KEY,
  couple_space_id BIGINT NOT NULL REFERENCES couple_spaces(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  date DATE NOT NULL,
  repeat_type TEXT NOT NULL DEFAULT 'yearly',
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (repeat_type IN ('none', 'yearly', 'monthly'))
);

CREATE TABLE IF NOT EXISTS bucket_items (
  id BIGSERIAL PRIMARY KEY,
  couple_space_id BIGINT NOT NULL REFERENCES couple_spaces(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'want',
  target_date DATE,
  completed_memory_id BIGINT REFERENCES memories(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (status IN ('want', 'planned', 'done'))
);

CREATE TABLE IF NOT EXISTS memory_tags (
  id BIGSERIAL PRIMARY KEY,
  memory_id BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  UNIQUE(memory_id, tag)
);

CREATE TABLE IF NOT EXISTS share_links (
  id BIGSERIAL PRIMARY KEY,
  couple_space_id BIGINT NOT NULL REFERENCES couple_spaces(id) ON DELETE CASCADE,
  memory_id BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  token TEXT NOT NULL UNIQUE,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memories_space_date ON memories(couple_space_id, memory_date DESC);
CREATE INDEX IF NOT EXISTS idx_photos_space_taken_at ON photos(couple_space_id, taken_at DESC);
CREATE INDEX IF NOT EXISTS idx_anniversaries_space_date ON anniversaries(couple_space_id, date);
CREATE INDEX IF NOT EXISTS idx_bucket_items_space_status ON bucket_items(couple_space_id, status);
