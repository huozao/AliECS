-- db/migrations/0037_version_inventory.sql
-- 全设备版本看板：组件登记表 + 上报表 + 上游对比表。复用备份看板 backup_policies 做设备心跳。
CREATE TABLE IF NOT EXISTS version_components (
    component_key   text PRIMARY KEY,
    display_name    text NOT NULL,
    kind            text NOT NULL DEFAULT 'docker-image',  -- docker-image | apt-summary | binary
    match_images    text[] NOT NULL DEFAULT '{}',
    devices         text[],                                 -- NULL=任意设备；用于区分同名镜像跨机（postgres）
    upstream_source text NOT NULL DEFAULT 'none',           -- github-release | dockerhub | none
    upstream_ref    text,                                   -- 'immich-app/immich' | 'library/postgres'
    version_pattern text,                                   -- 版本提取/比较正则；postgres 锁 '^16\.'
    pin_note        text,
    family          text NOT NULL DEFAULT 'third-party',    -- own | third-party | os
    sort_order      int NOT NULL DEFAULT 100,
    active          boolean NOT NULL DEFAULT TRUE,
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS version_reports (
    id          bigserial PRIMARY KEY,
    device      text NOT NULL,
    image       text NOT NULL,
    tag         text,
    digest      text,
    extra_json  jsonb NOT NULL DEFAULT '{}'::jsonb,
    reported_at timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_version_reports_device_time
    ON version_reports (device, reported_at DESC);

CREATE TABLE IF NOT EXISTS version_upstream_state (
    component_key text PRIMARY KEY REFERENCES version_components(component_key) ON DELETE CASCADE,
    latest_version text,
    release_url    text,
    checked_at     timestamptz,
    check_status   text,       -- ok | error
    check_error    text
);

-- 组件种子（来源：2026-07-17 实测容器清单）
INSERT INTO version_components
    (component_key, display_name, kind, match_images, devices, upstream_source, upstream_ref, version_pattern, pin_note, family, sort_order)
VALUES
    ('openclaw', 'OpenClaw 网关', 'docker-image', '{ghcr.io/openclaw/openclaw}', '{aliecs}',
     'github-release', 'openclaw/openclaw', NULL, '镜像按 sha256 锁定，实际版本取自 extra_json（容器内 exec 采集）', 'third-party', 10),
    ('authelia', 'Authelia SSO', 'docker-image', '{ghcr.io/authelia/authelia}', '{aliecs}',
     'github-release', 'authelia/authelia', NULL, NULL, 'third-party', 20),
    ('lldap', 'lldap 账号目录', 'docker-image', '{lldap/lldap}', '{aliecs}',
     'github-release', 'lldap/lldap', NULL, NULL, 'third-party', 21),
    ('postgres-aliecs', 'PostgreSQL（生产）', 'docker-image', '{postgres}', '{aliecs}',
     'dockerhub', 'library/postgres', '^16\.', '锁 16 大版本，只比对 16.x 内小版本升级', 'third-party', 30),
    ('immich-server', 'Immich 服务端', 'docker-image', '{ghcr.io/immich-app/immich-server}', '{webdock1}',
     'github-release', 'immich-app/immich', NULL, 'CVE 活跃，重点跟进', 'third-party', 40),
    ('immich-ml', 'Immich 机器学习', 'docker-image', '{ghcr.io/immich-app/immich-machine-learning}', '{webdock1}',
     'github-release', 'immich-app/immich', NULL, '版本随 immich-server 同步', 'third-party', 41),
    ('immich-postgres', 'Immich 数据库', 'docker-image', '{ghcr.io/immich-app/postgres,tensorchord/pgvecto-rs}', '{webdock1}',
     'none', NULL, NULL, '跟随 Immich 官方 compose 指定版本，不独立升级', 'third-party', 42),
    ('immich-redis', 'Immich Redis', 'docker-image', '{redis,valkey/valkey,docker.io/valkey/valkey}', '{webdock1}',
     'none', NULL, NULL, '跟随 Immich 官方 compose', 'third-party', 43),
    ('adventurelog-frontend', 'AdventureLog 前端', 'docker-image', '{ghcr.io/seanmorley15/adventurelog-frontend}', '{webdock1}',
     'github-release', 'seanmorley15/AdventureLog', NULL, NULL, 'third-party', 50),
    ('adventurelog-backend', 'AdventureLog 后端', 'docker-image', '{ghcr.io/seanmorley15/adventurelog-backend}', '{webdock1}',
     'github-release', 'seanmorley15/AdventureLog', NULL, NULL, 'third-party', 51),
    ('gokapi', 'Gokapi 文件分享', 'docker-image', '{f0rc3/gokapi,ghcr.io/forceu/gokapi}', '{webdock1}',
     'github-release', 'forceu/gokapi', NULL, NULL, 'third-party', 60),
    ('sing-box', 'sing-box', 'docker-image', '{ghcr.io/sagernet/sing-box}', '{aliecs}',
     'github-release', 'SagerNet/sing-box', NULL, NULL, 'third-party', 70),
    ('aliecs-services', 'AliECS 业务镜像', 'docker-image',
     '{ghcr.io/huozao/backend-api,ghcr.io/huozao/public-web,ghcr.io/huozao/admin-ui}', '{aliecs}',
     'none', NULL, NULL, '自家镜像，release 自动部署最新，无需上游对比', 'own', 80),
    ('openclaw-bridge', 'OpenClaw Bridge', 'docker-image', '{ghcr.io/huozao/openclaw-bridge}', '{aliecs}',
     'none', NULL, NULL, '自家镜像，手动 cutover', 'own', 81),
    ('webdock', 'WebDock 节点镜像', 'docker-image', '{ghcr.io/huozao/webdock}', '{webdock1,webdock2}',
     'none', NULL, NULL, '自家镜像，两机应保持同 tag（一致性核对）', 'own', 82),
    ('apt-summary', 'APT 可升级包', 'apt-summary', '{}', NULL,
     'none', NULL, NULL, '仅显示可升级数量与 security 数', 'os', 90)
ON CONFLICT (component_key) DO UPDATE SET
    display_name=EXCLUDED.display_name, kind=EXCLUDED.kind, match_images=EXCLUDED.match_images,
    devices=EXCLUDED.devices, upstream_source=EXCLUDED.upstream_source, upstream_ref=EXCLUDED.upstream_ref,
    version_pattern=EXCLUDED.version_pattern, pin_note=EXCLUDED.pin_note, family=EXCLUDED.family,
    sort_order=EXCLUDED.sort_order, updated_at=NOW();

-- 设备心跳：复用 backup_policies 的 stale 告警。采集脚本成功后 report 一笔 run。
INSERT INTO backup_policies
    (code, name, purpose, asset, source_device, method, schedule_label,
     expected_interval_seconds, warning_after_seconds, failure_after_seconds,
     retention_policy, lifecycle_status, monitoring_required, sort_order, detail_json)
VALUES
    ('version-inventory-aliecs', '版本采集心跳（aliecs）', '确认 aliecs 每日版本采集脚本在跑',
     'aliecs 容器/apt 版本快照', 'aliecs', 'systemd timer 每日采集上报', '每日 05:00',
     86400, 172800, 259200, '仅保留最新快照', 'active', TRUE, 100, '{}'::jsonb),
    ('version-inventory-webdock1', '版本采集心跳（webdock1）', '确认 webdock1 每日版本采集脚本在跑',
     'webdock1 容器/apt 版本快照', 'webdock1', 'systemd timer 每日采集上报', '每日 05:10',
     86400, 172800, 259200, '仅保留最新快照', 'active', TRUE, 101, '{}'::jsonb),
    ('version-inventory-webdock2', '版本采集心跳（webdock2）', '确认 webdock2 每日版本采集脚本在跑',
     'webdock2 容器/apt 版本快照', 'webdock2', 'systemd timer 每日采集上报', '每日 05:20',
     86400, 172800, 259200, '仅保留最新快照', 'active', TRUE, 102, '{}'::jsonb)
ON CONFLICT (code) DO UPDATE SET
    name=EXCLUDED.name, purpose=EXCLUDED.purpose, asset=EXCLUDED.asset,
    source_device=EXCLUDED.source_device, method=EXCLUDED.method, schedule_label=EXCLUDED.schedule_label,
    expected_interval_seconds=EXCLUDED.expected_interval_seconds,
    warning_after_seconds=EXCLUDED.warning_after_seconds, failure_after_seconds=EXCLUDED.failure_after_seconds,
    retention_policy=EXCLUDED.retention_policy, lifecycle_status=EXCLUDED.lifecycle_status,
    monitoring_required=EXCLUDED.monitoring_required, sort_order=EXCLUDED.sort_order,
    detail_json=EXCLUDED.detail_json, updated_at=NOW();
