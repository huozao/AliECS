-- txecs 流量看护使用独立来源，但复用已验证的 txecs-disk token 和飞书目标。
-- token 只从既有来源复制 hash，不把明文凭据写入迁移。
INSERT INTO notify_sources (source_key, token_sha256, enabled, note)
SELECT 'txecs-traffic', token_sha256, TRUE,
       'txecs eth0 出方向流量看护；token 与 txecs-disk 共用，设备侧 source 独立'
FROM notify_sources
WHERE source_key = 'txecs-disk'
ON CONFLICT (source_key) DO UPDATE SET
    token_sha256 = EXCLUDED.token_sha256,
    enabled = TRUE,
    note = EXCLUDED.note;

-- 同群、分消息：复制 txecs-disk 的全部飞书目标，保持目标标识不进代码。
INSERT INTO notify_routes
    (source_key, event_pattern, min_level, channel, target_json, enabled, sort_order, note)
SELECT 'txecs-traffic', r.event_pattern, r.min_level, r.channel, r.target_json,
       r.enabled, r.sort_order, 'txecs 流量看护；与 txecs-disk 同群但独立 source'
FROM notify_routes r
WHERE r.source_key = 'txecs-disk'
  AND NOT EXISTS (
      SELECT 1 FROM notify_routes existing
      WHERE existing.source_key = 'txecs-traffic'
        AND existing.event_pattern = r.event_pattern
        AND existing.channel = r.channel
        AND existing.target_json = r.target_json
  );
