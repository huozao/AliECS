ALTER TABLE gold_spread_alerts
    DROP CONSTRAINT IF EXISTS ck_gold_spread_alert_kind;

ALTER TABLE gold_spread_alerts
    ADD CONSTRAINT ck_gold_spread_alert_kind CHECK (
        kind IN ('anomaly_started', 'anomaly_escalated', 'anomaly_recovered',
                 'wrong_price_detected', 'historical_complete', 'historical_failed')
    );
