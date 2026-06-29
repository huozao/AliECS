CREATE TABLE IF NOT EXISTS image_backfill_targets (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    env_profile TEXT NOT NULL,
    external_doc_id TEXT NOT NULL,
    sheet_title TEXT NOT NULL,
    attachment_field_title TEXT NOT NULL,
    image_field_title TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_image_backfill_targets_identity
    ON image_backfill_targets(provider, env_profile, external_doc_id, sheet_title, attachment_field_title, image_field_title);

INSERT INTO image_backfill_targets(
    provider, env_profile, external_doc_id, sheet_title,
    attachment_field_title, image_field_title, enabled, updated_at
)
VALUES (
    'wecom',
    'COMPANY_B',
    'dc45aaSDeAwXO54CKSmFkl3ZOH8H_MLVqEmnfE07PONMKTJGB_4T_d5_8LKzdJ7QB2x7lfi8fQkghPaG5gKWyWLA',
    '配色&样品需求单',
    '附件',
    '图片',
    TRUE,
    NOW()
)
ON CONFLICT(provider, env_profile, external_doc_id, sheet_title, attachment_field_title, image_field_title)
DO UPDATE SET
    image_field_title = EXCLUDED.image_field_title,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();
