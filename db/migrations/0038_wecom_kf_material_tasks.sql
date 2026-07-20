CREATE TABLE IF NOT EXISTS wecom_kf_cursors (
    open_kfid TEXT PRIMARY KEY,
    cursor TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wecom_kf_material_tasks (
    id BIGSERIAL PRIMARY KEY,
    task_key TEXT NOT NULL UNIQUE,
    open_kfid TEXT NOT NULL,
    external_userid TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'collecting'
        CHECK (status IN (
            'collecting', 'analyzing', 'awaiting_confirmation',
            'executing', 'completed', 'cancelled', 'failed'
        )),
    analysis_text TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    confirmed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_wecom_kf_material_tasks_active
    ON wecom_kf_material_tasks(open_kfid, external_userid)
    WHERE status IN ('collecting', 'analyzing', 'awaiting_confirmation', 'executing', 'failed');

CREATE INDEX IF NOT EXISTS ix_wecom_kf_material_tasks_user_time
    ON wecom_kf_material_tasks(open_kfid, external_userid, id DESC);

CREATE TABLE IF NOT EXISTS wecom_kf_material_items (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL REFERENCES wecom_kf_material_tasks(id) ON DELETE CASCADE,
    msgid TEXT NOT NULL UNIQUE,
    msgtype TEXT NOT NULL,
    text_content TEXT NOT NULL DEFAULT '',
    original_filename TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    byte_size BIGINT NOT NULL DEFAULT 0 CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL DEFAULT '',
    storage_path TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_wecom_kf_material_items_task
    ON wecom_kf_material_items(task_id, id);

CREATE TABLE IF NOT EXISTS wecom_kf_outbound_messages (
    msgid TEXT PRIMARY KEY,
    task_id BIGINT REFERENCES wecom_kf_material_tasks(id) ON DELETE SET NULL,
    open_kfid TEXT NOT NULL,
    external_userid TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'sent', 'failed')),
    fail_type INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_wecom_kf_outbound_messages_task
    ON wecom_kf_outbound_messages(task_id, created_at DESC);
