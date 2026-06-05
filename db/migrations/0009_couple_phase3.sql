-- Phase 3: Couple Memory 完整闭环增量

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE memories
  ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE share_links
  ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_photos_memory ON photos(memory_id);
CREATE INDEX IF NOT EXISTS idx_couple_members_user ON couple_members(user_id, couple_space_id);
CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag);

INSERT INTO permissions(code, name, description)
VALUES ('couple_memory_access', 'Couple Memory 访问', '访问双人私密回忆空间')
ON CONFLICT(code) DO NOTHING;

INSERT INTO roles(code, name, description)
VALUES ('couple_memory', 'Couple Memory 成员', '可访问 Couple Memory 私密空间')
ON CONFLICT(code) DO NOTHING;

INSERT INTO role_permissions(role_id, permission_id)
SELECT r.id, p.id
FROM roles r, permissions p
WHERE r.code = 'couple_memory' AND p.code = 'couple_memory_access'
ON CONFLICT DO NOTHING;

INSERT INTO schema_migrations(version)
VALUES ('0009_couple_phase3')
ON CONFLICT(version) DO NOTHING;
