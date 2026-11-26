ALTER TABLE notify_deliveries
    ADD COLUMN IF NOT EXISTS feishu_recovery_message_id TEXT,
    ADD COLUMN IF NOT EXISTS feishu_recovery_qr_attached BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS feishu_recovery_card_state TEXT,
    ADD COLUMN IF NOT EXISTS feishu_recovery_retry_after TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_notify_feishu_recovery_deliveries
    ON notify_deliveries (outbox_id)
    WHERE feishu_recovery_message_id IS NOT NULL;
