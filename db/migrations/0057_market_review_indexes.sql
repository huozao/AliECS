-- Keep incremental review reads bounded as the append-only stream grows.
CREATE INDEX IF NOT EXISTS market_review_snapshots_run_time
    ON market_review_snapshots (run_id, published_at DESC, sequence DESC);
