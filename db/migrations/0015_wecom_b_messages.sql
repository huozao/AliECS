CREATE TABLE IF NOT EXISTS wecom_b_messages (
    id BIGSERIAL PRIMARY KEY,
    msg_id TEXT NOT NULL UNIQUE,
    bot_id TEXT,
    chat_id TEXT,
    chat_type TEXT,
    sender_id TEXT,
    msg_type TEXT,
    content TEXT,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wecom_b_messages_chat_received
    ON wecom_b_messages(chat_id, received_at DESC);
