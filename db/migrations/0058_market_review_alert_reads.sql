-- Per-user acknowledgement state for live target-fill reminders.
-- Reading an alert never closes a position or changes the immutable event.
CREATE TABLE IF NOT EXISTS market_review_event_reads (
    user_id BIGINT NOT NULL,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES market_review_events(event_id),
    read_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_id, event_id)
);
CREATE INDEX IF NOT EXISTS market_review_event_reads_run
    ON market_review_event_reads (user_id, run_id, read_at DESC);
