-- Short-lived codes only; reusable sessions never travel in redirect URLs.
CREATE TABLE IF NOT EXISTS auth_browser_handoffs (
    code_hash TEXT PRIMARY KEY,
    challenge TEXT NOT NULL,
    origin TEXT NOT NULL,
    session_token TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_browser_handoffs_expiry_idx ON auth_browser_handoffs (expires_at);
