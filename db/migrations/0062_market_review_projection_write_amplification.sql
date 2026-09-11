-- migration: atomic
BEGIN;

-- The 0059 trigger persisted every accepted intermediate observation.  A
-- packet contains many observations for the same current key, so that caused
-- one heap update per array item and eventually made the 15 s ingest budget
-- expire.  Keep the immutable observation insert unchanged, but reduce each
-- packet/key to one final upsert after replaying the same acceptance rule in
-- memory.
LOCK TABLE market_review_snapshots IN SHARE ROW EXCLUSIVE MODE;

CREATE OR REPLACE FUNCTION market_review_project_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    key_row RECORD;
    observation market_review_observations;
    winner market_review_observations;
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

    -- The old trigger replayed only this packet, in field/ordinal order.  The
    -- current row is the starting winner; each key is written once below.
    FOR key_row IN
        SELECT DISTINCT field,contract
        FROM market_review_observations
        WHERE run_id=NEW.run_id AND sequence=NEW.sequence
        ORDER BY field,contract
    LOOP
        winner := NULL;
        SELECT o.* INTO winner
        FROM market_review_current_observations c
        JOIN market_review_observations o
          ON o.run_id=c.run_id AND o.sequence=c.sequence
         AND o.field=c.field AND o.ordinal=c.ordinal
        WHERE c.run_id=NEW.run_id AND c.field=key_row.field
          AND c.contract=key_row.contract;

        FOR observation IN
            SELECT * FROM market_review_observations
            WHERE run_id=NEW.run_id AND sequence=NEW.sequence
              AND field=key_row.field AND contract=key_row.contract
            ORDER BY ordinal
        LOOP
            IF winner.run_id IS NULL OR
               (COALESCE(observation.source_time,'-infinity'::timestamptz)
                    >= COALESCE(winner.source_time,'-infinity'::timestamptz)
                AND COALESCE(observation.observed_at,'-infinity'::timestamptz)
                    >= COALESCE(winner.observed_at,'-infinity'::timestamptz)
                AND (COALESCE(observation.source_time,'-infinity'::timestamptz),
                     COALESCE(observation.observed_at,'-infinity'::timestamptz),
                     observation.sequence,observation.ordinal)
                    > (COALESCE(winner.source_time,'-infinity'::timestamptz),
                       COALESCE(winner.observed_at,'-infinity'::timestamptz),
                       winner.sequence,winner.ordinal)) THEN
                winner := observation;
            END IF;
        END LOOP;

        IF winner.run_id IS NOT NULL THEN
            PERFORM market_review_advance_observation(winner);
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

COMMIT;
