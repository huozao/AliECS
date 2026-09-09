-- migration: atomic
BEGIN;
-- Freeze old-writer INSERTs until both the run watermarks and all historical
-- gaps exist. Without this lock, a new row for legacy {1,3} could initialize a
-- watermark at 4 and incorrectly retain [1,3] as missing.
LOCK TABLE market_review_events IN SHARE ROW EXCLUSIVE MODE;

-- Incremental gap projection; original events remain append-only evidence.
-- Queries never scan old event streams to reconstruct missing sequence numbers.
CREATE TABLE IF NOT EXISTS market_review_event_watermarks (
    run_id TEXT PRIMARY KEY,
    max_sequence BIGINT NOT NULL CHECK (max_sequence >= 0)
);
CREATE TABLE IF NOT EXISTS market_review_event_gaps (
    run_id TEXT NOT NULL REFERENCES market_review_event_watermarks(run_id),
    start_sequence BIGINT NOT NULL CHECK (start_sequence > 0),
    end_sequence BIGINT NOT NULL CHECK (end_sequence >= start_sequence),
    PRIMARY KEY (run_id,start_sequence)
);

-- Lock before unique event indexes are touched. With only an AFTER-row lock,
-- overlapping old-writer batches can hold each other's event index and run row.
-- Match the application's existing run lock; duplicate INSERTs only take a
-- lock, while projection mutation still happens exclusively AFTER insertion.
CREATE OR REPLACE FUNCTION market_review_lock_event_run() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('market:'||NEW.run_id,0));
    RETURN NEW;
END;
$$;
CREATE OR REPLACE TRIGGER market_review_event_run_lock
    BEFORE INSERT ON market_review_events
    FOR EACH ROW EXECUTE FUNCTION market_review_lock_event_run();

CREATE OR REPLACE FUNCTION market_review_project_event_gap() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    high_sequence BIGINT;
    gap_start BIGINT;
    gap_end BIGINT;
BEGIN
    INSERT INTO market_review_event_watermarks(run_id,max_sequence)
        VALUES(NEW.run_id,0) ON CONFLICT DO NOTHING;
    -- Both old and new backend writers serialize against this one run row.
    -- Unrelated runs remain independent; no full event history is locked/read.
    SELECT max_sequence INTO high_sequence FROM market_review_event_watermarks
        WHERE run_id=NEW.run_id FOR UPDATE;
    IF NEW.sequence > high_sequence THEN
        IF NEW.sequence > high_sequence + 1 THEN
            INSERT INTO market_review_event_gaps(run_id,start_sequence,end_sequence)
                VALUES(NEW.run_id,high_sequence+1,NEW.sequence-1);
        END IF;
        UPDATE market_review_event_watermarks SET max_sequence=NEW.sequence
            WHERE run_id=NEW.run_id;
    ELSE
        -- At most one indexed predecessor can contain this late sequence.
        SELECT start_sequence,end_sequence INTO gap_start,gap_end
            FROM market_review_event_gaps
            WHERE run_id=NEW.run_id AND start_sequence<=NEW.sequence
            ORDER BY start_sequence DESC LIMIT 1 FOR UPDATE;
        IF FOUND AND gap_end>=NEW.sequence THEN
            DELETE FROM market_review_event_gaps
                WHERE run_id=NEW.run_id AND start_sequence=gap_start;
            IF gap_start < NEW.sequence THEN
                INSERT INTO market_review_event_gaps(run_id,start_sequence,end_sequence)
                    VALUES(NEW.run_id,gap_start,NEW.sequence-1);
            END IF;
            IF NEW.sequence < gap_end THEN
                INSERT INTO market_review_event_gaps(run_id,start_sequence,end_sequence)
                    VALUES(NEW.run_id,NEW.sequence+1,gap_end);
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE TRIGGER market_review_event_gap_projection
    AFTER INSERT ON market_review_events
    FOR EACH ROW EXECUTE FUNCTION market_review_project_event_gap();

-- Only runs with no projection are backfilled. Reapplying cannot recreate gaps
-- already resolved by late events. One window scan is allowed at migration time.
WITH new_runs AS (
    INSERT INTO market_review_event_watermarks(run_id,max_sequence)
        SELECT run_id,max(sequence) FROM market_review_events GROUP BY run_id
        ON CONFLICT DO NOTHING RETURNING run_id
), ordered_events AS (
    SELECT e.run_id,e.sequence,
        lag(e.sequence,1,0) OVER(PARTITION BY e.run_id ORDER BY e.sequence) previous
    FROM market_review_events e JOIN new_runs n ON n.run_id=e.run_id
)
INSERT INTO market_review_event_gaps(run_id,start_sequence,end_sequence)
    SELECT run_id,previous+1,sequence-1 FROM ordered_events
    WHERE sequence>previous+1 ON CONFLICT DO NOTHING;
COMMIT;
