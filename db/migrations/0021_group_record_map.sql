CREATE TABLE IF NOT EXISTS group_record_map (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'wecom',
    env_profile TEXT NOT NULL DEFAULT '',
    chatid TEXT NOT NULL,
    external_doc_id TEXT NOT NULL DEFAULT '',
    sheet_title TEXT NOT NULL DEFAULT '',
    record_id TEXT NOT NULL DEFAULT '',
    requirement_key TEXT NOT NULL DEFAULT '',
    bound_by TEXT NOT NULL DEFAULT '',
    bound_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_group_record_map_chatid ON group_record_map(chatid);
