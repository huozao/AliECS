CREATE TABLE IF NOT EXISTS group_messages (
    id SERIAL PRIMARY KEY,
    msgid TEXT NOT NULL,
    chatid TEXT NOT NULL DEFAULT '',
    from_userid TEXT NOT NULL DEFAULT '',
    msgtype TEXT NOT NULL DEFAULT '',
    text_content TEXT NOT NULL DEFAULT '',
    quote_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    media_paths JSONB NOT NULL DEFAULT '[]'::jsonb,
    record_id TEXT NOT NULL DEFAULT '',
    is_node BOOLEAN NOT NULL DEFAULT FALSE,
    node_category TEXT NOT NULL DEFAULT '',
    node_summary TEXT NOT NULL DEFAULT '',
    written_to_sheet BOOLEAN NOT NULL DEFAULT FALSE,
    ts TIMESTAMPTZ,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_group_messages_msgid ON group_messages(msgid);
CREATE INDEX IF NOT EXISTS ix_group_messages_chatid ON group_messages(chatid);
CREATE INDEX IF NOT EXISTS ix_group_messages_record_id ON group_messages(record_id);
