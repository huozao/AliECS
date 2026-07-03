ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_sub TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS users_oidc_sub_key ON users (oidc_sub) WHERE oidc_sub IS NOT NULL;
