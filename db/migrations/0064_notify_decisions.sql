-- 统一消息中枢：飞书卡片决策按钮支持。
-- notify_decisions 记录决策实体，notify_decision_tokens 记录每次发卡生成的链接令牌（只存 sha256），
-- notify_decision_events 记录用户改选事件历史。
-- 同一决策可多次发卡（重试、提醒），每张卡的令牌都保持有效：令牌单独成表，只增不覆盖。

CREATE TABLE IF NOT EXISTS notify_decisions (
    id             BIGSERIAL PRIMARY KEY,
    source_key     TEXT NOT NULL,
    decision_key   TEXT NOT NULL,
    question       TEXT NOT NULL,
    options_json   JSONB NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    chosen_option  TEXT,
    chosen_label   TEXT,
    chosen_at      TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_key, decision_key)
);

CREATE INDEX IF NOT EXISTS idx_notify_decisions_source_status
    ON notify_decisions (source_key, status, created_at DESC);

CREATE TABLE IF NOT EXISTS notify_decision_tokens (
    id            BIGSERIAL PRIMARY KEY,
    decision_id   BIGINT NOT NULL REFERENCES notify_decisions(id) ON DELETE CASCADE,
    token_sha256  TEXT NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notify_decision_tokens_decision
    ON notify_decision_tokens (decision_id);

CREATE TABLE IF NOT EXISTS notify_decision_events (
    id            BIGSERIAL PRIMARY KEY,
    decision_id   BIGINT NOT NULL REFERENCES notify_decisions(id) ON DELETE CASCADE,
    option_key    TEXT NOT NULL,
    option_label  TEXT NOT NULL,
    client_ip     TEXT NOT NULL DEFAULT '',
    user_agent    TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notify_decision_events_decision
    ON notify_decision_events (decision_id, created_at ASC);
