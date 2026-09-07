-- Append-only market evidence. No trading tables or execution permissions.
CREATE TABLE IF NOT EXISTS market_review_snapshots (
    run_id TEXT NOT NULL, sequence BIGINT NOT NULL CHECK (sequence >= 0),
    published_at TIMESTAMPTZ NOT NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    body JSONB NOT NULL, PRIMARY KEY (run_id, sequence)
);
CREATE INDEX IF NOT EXISTS market_review_snapshots_time ON market_review_snapshots (published_at DESC);
CREATE TABLE IF NOT EXISTS market_review_events (
    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence BIGINT NOT NULL CHECK (sequence > 0),
    position_id TEXT, occurred_at TIMESTAMPTZ NOT NULL, body JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), UNIQUE (run_id, sequence)
);
CREATE INDEX IF NOT EXISTS market_review_events_position ON market_review_events(position_id, run_id, sequence);
CREATE TABLE IF NOT EXISTS market_review_positions (
    position_id TEXT NOT NULL, run_id TEXT NOT NULL, body JSONB NOT NULL,
    unresolved BOOLEAN NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, position_id)
);
CREATE INDEX IF NOT EXISTS market_review_unresolved ON market_review_positions(updated_at DESC) WHERE unresolved;
CREATE TABLE IF NOT EXISTS market_review_annotations (
    id BIGSERIAL PRIMARY KEY, position_id TEXT NOT NULL, run_id TEXT NOT NULL,
    revision INTEGER NOT NULL, author_id BIGINT NOT NULL, author_name TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('confirmed','uncertain','excluded')),
    reason TEXT NOT NULL, evidence_ids JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), UNIQUE (run_id,position_id,revision)
);
INSERT INTO permissions(code,name,description) VALUES
 ('market.read','行情观察读取','读取价格带、交易证据与双账'),
 ('market.annotate','行情人工标注','追加独立人工判断，不允许交易')
ON CONFLICT(code) DO NOTHING;
INSERT INTO role_permissions(role_id,permission_id)
 SELECT r.id,p.id FROM roles r CROSS JOIN permissions p
 WHERE r.code='admin' AND p.code IN ('market.read','market.annotate')
ON CONFLICT DO NOTHING;
