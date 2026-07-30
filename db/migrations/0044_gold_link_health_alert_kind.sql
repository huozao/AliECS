-- 监控端早就在发 data_silence / data_silence_recovered / replay_summary 三类链路
-- 健康告警，服务端 schema 与 CHECK 都不认，全部 422 退回、只落在监控端 pending 队列
-- （2026-07-29/07-30 实测积压 3 条）。这里放宽 kind 与 source 白名单。
ALTER TABLE gold_spread_alerts
    DROP CONSTRAINT IF EXISTS ck_gold_spread_alert_kind;

ALTER TABLE gold_spread_alerts
    ADD CONSTRAINT ck_gold_spread_alert_kind CHECK (
        kind IN ('anomaly_started', 'anomaly_escalated', 'anomaly_recovered',
                 'wrong_price_detected', 'wrong_price_review',
                 'historical_complete', 'historical_failed',
                 'data_silence', 'data_silence_recovered', 'replay_summary')
    );

-- replay_summary 由收盘复盘链路发出，source 是 replay。
ALTER TABLE gold_spread_alerts
    DROP CONSTRAINT IF EXISTS ck_gold_spread_alert_source;

ALTER TABLE gold_spread_alerts
    ADD CONSTRAINT ck_gold_spread_alert_source CHECK (
        source IN ('live', 'historical', 'replay')
    );
