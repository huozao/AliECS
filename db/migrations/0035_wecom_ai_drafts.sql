CREATE TABLE IF NOT EXISTS wecom_ai_drafts (
    id BIGSERIAL PRIMARY KEY,
    source_msgid TEXT NOT NULL UNIQUE REFERENCES group_messages(msgid) ON DELETE CASCADE,
    chatid TEXT NOT NULL,
    from_userid TEXT NOT NULL DEFAULT '',
    node_category TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL DEFAULT '',
    result_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'analyzing'
        CHECK (status IN ('analyzing', 'ready', 'confirmed', 'cancelled')),
    confirmed_by TEXT NOT NULL DEFAULT '',
    confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_wecom_ai_drafts_chat_status
    ON wecom_ai_drafts(chatid, from_userid, status, id DESC);
