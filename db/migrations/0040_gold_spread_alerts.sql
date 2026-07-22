CREATE TABLE IF NOT EXISTS gold_spread_alerts (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    rendered_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    CONSTRAINT ck_gold_spread_alert_kind CHECK (
        kind IN ('anomaly_started', 'anomaly_escalated', 'anomaly_recovered',
                 'historical_complete', 'historical_failed')
    ),
    CONSTRAINT ck_gold_spread_alert_source CHECK (source IN ('live', 'historical')),
    CONSTRAINT ck_gold_spread_alert_severity CHECK (severity IN ('info', 'warning', 'critical')),
    CONSTRAINT ck_gold_spread_alert_status CHECK (status IN ('pending', 'sent', 'failed'))
);

CREATE INDEX IF NOT EXISTS ix_gold_spread_alerts_time
    ON gold_spread_alerts(occurred_at DESC);

CREATE INDEX IF NOT EXISTS ix_gold_spread_alerts_failed
    ON gold_spread_alerts(updated_at)
    WHERE status = 'failed';
