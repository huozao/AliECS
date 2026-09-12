-- devbox 代理流量采集器与日报统一走 notify-center。
-- 独立 source/token，收件人仍由中枢路由表持有；脚本不再保存飞书 app 凭据。
INSERT INTO notify_sources (source_key, token_sha256, enabled, note)
VALUES (
    'devbox-traffic',
    '49019585e2cd4b8a5a48e9fb415b5dbe580da633e0e49ad95e42427949994b9a',
    TRUE,
    'devbox 代理流量采集器与日报；通过 notify-center 投递，飞书卡片可原生透传'
)
ON CONFLICT (source_key) DO UPDATE SET
    token_sha256 = EXCLUDED.token_sha256,
    enabled = TRUE,
    note = EXCLUDED.note;

INSERT INTO notify_routes
    (source_key, event_pattern, min_level, channel, target_json, enabled, sort_order, note)
SELECT
    'devbox-traffic',
    'traffic.*',
    'info',
    'feishu',
    '{"profile":"COMPANY_A","receive_id":"oc_84d1130542509e374f7ea20c13d11ca4","receive_id_type":"chat_id"}'::jsonb,
    TRUE,
    100,
    'devbox 代理流量通知；目标保持迁移前的运维群'
WHERE NOT EXISTS (
    SELECT 1 FROM notify_routes
    WHERE source_key = 'devbox-traffic'
      AND event_pattern = 'traffic.*'
      AND channel = 'feishu'
      AND target_json = '{"profile":"COMPANY_A","receive_id":"oc_84d1130542509e374f7ea20c13d11ca4","receive_id_type":"chat_id"}'::jsonb
);
