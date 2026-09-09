-- migration: atomic
BEGIN;
-- Block old and new snapshot writers until the trigger and complete backfill
-- become visible together. The outer runner may use psql autocommit, so this
-- migration owns its own atomic boundary.
LOCK TABLE market_review_snapshots IN SHARE ROW EXCLUSIVE MODE;

-- Derived query indexes only. Original snapshot JSON remains immutable evidence.
-- Existing packets are unpacked once here, never on a market query. Re-running
-- is safe; invalid legacy timestamps fail the migration instead of deleting data.
CREATE TABLE IF NOT EXISTS market_review_observations (
    run_id TEXT NOT NULL, sequence BIGINT NOT NULL,
    field TEXT NOT NULL CHECK (field IN ('quotes','bands')),
    ordinal BIGINT NOT NULL, contract TEXT NOT NULL,
    source_time TIMESTAMPTZ, observed_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ NOT NULL, body JSONB NOT NULL,
    PRIMARY KEY (run_id,sequence,field,ordinal),
    FOREIGN KEY (run_id,sequence) REFERENCES market_review_snapshots(run_id,sequence)
);
CREATE INDEX IF NOT EXISTS market_review_observations_run_window
    ON market_review_observations(run_id,observed_at,sequence,field,ordinal);
CREATE INDEX IF NOT EXISTS market_review_observations_symbol_window
    ON market_review_observations(run_id,contract,observed_at,sequence,field,ordinal);
CREATE INDEX IF NOT EXISTS market_review_observations_window
    ON market_review_observations(observed_at,run_id,sequence,field,ordinal);

CREATE TABLE IF NOT EXISTS market_review_current_observations (
    run_id TEXT NOT NULL, field TEXT NOT NULL, contract TEXT NOT NULL,
    sequence BIGINT NOT NULL, ordinal BIGINT NOT NULL,
    source_time TIMESTAMPTZ, observed_at TIMESTAMPTZ, body JSONB NOT NULL,
    PRIMARY KEY (run_id,field,contract),
    FOREIGN KEY (run_id,sequence,field,ordinal)
        REFERENCES market_review_observations(run_id,sequence,field,ordinal)
);

-- Both clocks advance independently: a later domestic source must not bring
-- the display back to an older observation, or vice versa. Rejected current
-- updates still exist in immutable observation history.
CREATE OR REPLACE FUNCTION market_review_advance_observation(incoming market_review_observations)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO market_review_current_observations AS current
        (run_id,field,contract,sequence,ordinal,source_time,observed_at,body)
    VALUES (incoming.run_id,incoming.field,incoming.contract,incoming.sequence,
            incoming.ordinal,incoming.source_time,incoming.observed_at,incoming.body)
    ON CONFLICT (run_id,field,contract) DO UPDATE SET
        sequence=EXCLUDED.sequence, ordinal=EXCLUDED.ordinal,
        source_time=EXCLUDED.source_time, observed_at=EXCLUDED.observed_at, body=EXCLUDED.body
    WHERE COALESCE(EXCLUDED.source_time,'-infinity'::timestamptz)
            >= COALESCE(current.source_time,'-infinity'::timestamptz)
      AND COALESCE(EXCLUDED.observed_at,'-infinity'::timestamptz)
            >= COALESCE(current.observed_at,'-infinity'::timestamptz)
      AND (COALESCE(EXCLUDED.source_time,'-infinity'::timestamptz),
           COALESCE(EXCLUDED.observed_at,'-infinity'::timestamptz),EXCLUDED.sequence,EXCLUDED.ordinal)
        > (COALESCE(current.source_time,'-infinity'::timestamptz),
           COALESCE(current.observed_at,'-infinity'::timestamptz),current.sequence,current.ordinal);
$$;

-- Keep projections in the snapshot transaction even while the previous backend
-- version is still serving writes during rollout. No reader performs backfills.
CREATE OR REPLACE FUNCTION market_review_project_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE observation market_review_observations;
BEGIN
    INSERT INTO market_review_observations
        (run_id,sequence,field,ordinal,contract,source_time,observed_at,published_at,body)
    SELECT NEW.run_id,NEW.sequence,f.field,a.ordinal,a.item->>'contract',
        (a.item->>'source_time')::timestamptz,
        COALESCE(a.item->>'observed_at',a.item->>'captured_at',a.item->>'source_time')::timestamptz,
        NEW.published_at,a.item
    FROM (VALUES ('quotes'),('bands')) f(field)
    CROSS JOIN LATERAL jsonb_array_elements(NEW.body->f.field) WITH ORDINALITY a(item,ordinal)
    WHERE a.item->>'contract' IS NOT NULL
    ON CONFLICT DO NOTHING;

    FOR observation IN SELECT * FROM market_review_observations
        WHERE run_id=NEW.run_id AND sequence=NEW.sequence ORDER BY field,ordinal
    LOOP
        PERFORM market_review_advance_observation(observation);
    END LOOP;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE TRIGGER market_review_snapshot_projection
    AFTER INSERT ON market_review_snapshots
    FOR EACH ROW EXECUTE FUNCTION market_review_project_snapshot();

INSERT INTO market_review_observations
    (run_id,sequence,field,ordinal,contract,source_time,observed_at,published_at,body)
SELECT s.run_id,s.sequence,f.field,a.ordinal,a.item->>'contract',
    (a.item->>'source_time')::timestamptz,
    COALESCE(a.item->>'observed_at',a.item->>'captured_at',a.item->>'source_time')::timestamptz,
    s.published_at,a.item
FROM market_review_snapshots s
CROSS JOIN (VALUES ('quotes'),('bands')) f(field)
CROSS JOIN LATERAL jsonb_array_elements(s.body->f.field) WITH ORDINALITY a(item,ordinal)
WHERE a.item->>'contract' IS NOT NULL
ON CONFLICT DO NOTHING;

DO $$
DECLARE
    observation market_review_observations;
    winner market_review_observations;
BEGIN
    -- Replay the same per-key sequence/ordinal order in memory. Updating the
    -- same current row for every historical observation is the measured
    -- bottleneck (824036 rows exceeded 900s in rehearsal).
    -- Persist only the final accepted observation for each independent key.
    FOR observation IN SELECT * FROM market_review_observations
        ORDER BY run_id,field,contract,sequence,ordinal
    LOOP
        IF winner.run_id IS NOT NULL AND
           (winner.run_id,winner.field,winner.contract) IS DISTINCT FROM
           (observation.run_id,observation.field,observation.contract) THEN
            PERFORM market_review_advance_observation(winner);
            winner := NULL;
        END IF;
        IF winner.run_id IS NULL THEN
            -- A rerun starts from the already accepted current observation,
            -- exactly as the original row-by-row upsert did.
            SELECT o.* INTO winner FROM market_review_current_observations c
            JOIN market_review_observations o USING (run_id,sequence,field,ordinal)
            WHERE c.run_id=observation.run_id AND c.field=observation.field
              AND c.contract=observation.contract;
            IF NOT FOUND THEN
                winner := observation;
                CONTINUE;
            END IF;
        END IF;
        IF COALESCE(observation.source_time,'-infinity'::timestamptz)
                >= COALESCE(winner.source_time,'-infinity'::timestamptz)
           AND COALESCE(observation.observed_at,'-infinity'::timestamptz)
                >= COALESCE(winner.observed_at,'-infinity'::timestamptz)
           AND (COALESCE(observation.source_time,'-infinity'::timestamptz),
                COALESCE(observation.observed_at,'-infinity'::timestamptz),
                observation.sequence,observation.ordinal)
             > (COALESCE(winner.source_time,'-infinity'::timestamptz),
                COALESCE(winner.observed_at,'-infinity'::timestamptz),
                winner.sequence,winner.ordinal) THEN
            winner := observation;
        END IF;
    END LOOP;
    IF winner.run_id IS NOT NULL THEN
        PERFORM market_review_advance_observation(winner);
    END IF;
END;
$$;
COMMIT;
