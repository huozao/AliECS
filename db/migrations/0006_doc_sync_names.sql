ALTER TABLE external_sources
    ADD COLUMN IF NOT EXISTS document_name TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS sheet_name TEXT NOT NULL DEFAULT '';

UPDATE external_sources
SET
    document_name = COALESCE(NULLIF(document_name, ''), split_part(source_name, ' / ', 1)),
    sheet_name = COALESCE(
        NULLIF(sheet_name, ''),
        CASE
            WHEN position(' / ' in source_name) > 0 THEN split_part(source_name, ' / ', 2)
            ELSE ''
        END
    )
WHERE source_type = 'smartsheet_sheet';
