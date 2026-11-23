CREATE TABLE IF NOT EXISTS notify_wechat_payloads (
    delivery_id BIGINT PRIMARY KEY REFERENCES notify_deliveries(id) ON DELETE CASCADE,
    agent TEXT NOT NULL,
    ciphertext BYTEA,
    fingerprint TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    step_count SMALLINT NOT NULL CHECK (step_count BETWEEN 1 AND 8),
    lease TEXT,
    lease_until TIMESTAMPTZ,
    next_step SMALLINT NOT NULL DEFAULT 0,
    receipts JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS notify_wechat_agent_queue
    ON notify_wechat_payloads (agent, delivery_id) WHERE ciphertext IS NOT NULL;
