ALTER TABLE anniversaries
  ADD COLUMN IF NOT EXISTS event_type TEXT NOT NULL DEFAULT 'anniversary',
  ADD COLUMN IF NOT EXISTS calendar_type TEXT NOT NULL DEFAULT 'solar',
  ADD COLUMN IF NOT EXISTS lunar_month INTEGER,
  ADD COLUMN IF NOT EXISTS lunar_day INTEGER,
  ADD COLUMN IF NOT EXISTS lunar_leap_month BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE anniversaries
  DROP CONSTRAINT IF EXISTS anniversaries_event_type_check;
ALTER TABLE anniversaries
  ADD CONSTRAINT anniversaries_event_type_check CHECK (event_type IN ('anniversary', 'birthday'));
ALTER TABLE anniversaries
  DROP CONSTRAINT IF EXISTS anniversaries_calendar_type_check;
ALTER TABLE anniversaries
  ADD CONSTRAINT anniversaries_calendar_type_check CHECK (calendar_type IN ('solar', 'lunar'));
ALTER TABLE anniversaries
  DROP CONSTRAINT IF EXISTS anniversaries_lunar_fields_check;
ALTER TABLE anniversaries
  ADD CONSTRAINT anniversaries_lunar_fields_check CHECK (
    calendar_type = 'solar' OR (lunar_month BETWEEN 1 AND 12 AND lunar_day BETWEEN 1 AND 30)
  );

CREATE INDEX IF NOT EXISTS idx_anniversaries_space_type ON anniversaries(couple_space_id, event_type, calendar_type);
