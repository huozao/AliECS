-- 统一消息中枢：所有出站通知（飞书 / 企微群机器人 / 企微自建应用）的唯一汇聚点。
--
-- 汇聚方式刻意不用 HTTP：doc-sync-worker 与 backend-api 是两个镜像、构建上下文互不可见，
-- 走 HTTP 会给 worker 平添一个「backend-api 必须活着」的可用性依赖。两者连同一个库，
-- 所以生产者（含外部设备经 HTTP 入口）最终都只做一件事——往 notify_outbox 写一行。
--
-- 投递代码只存在于 backend-api 一份。触发有两条：HTTP 入口收到消息后同步投递；
-- worker 主循环每轮调一次 flush 端点带走它自己写的行（失败无所谓，行已落库，下一轮再带）。

-- 上行鉴权：一个来源一个 token，可单独吊销。token 只存 sha256。
CREATE TABLE IF NOT EXISTS notify_sources (
    source_key   TEXT PRIMARY KEY,
    token_sha256 TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 路由：一条消息可命中多个 target，各自独立投递、独立重试。
-- source_key / event_pattern 支持 '*' 通配；min_level 低于阈值的消息不进该 target。
CREATE TABLE IF NOT EXISTS notify_routes (
    id            BIGSERIAL PRIMARY KEY,
    source_key    TEXT NOT NULL DEFAULT '*',
    event_pattern TEXT NOT NULL DEFAULT '*',
    min_level     TEXT NOT NULL DEFAULT 'info',
    channel       TEXT NOT NULL,
    -- 只存凭据的「引用名」（profile / env 变量名）与收件人，绝不存密钥本身。
    target_json   JSONB NOT NULL,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order    INT NOT NULL DEFAULT 100,
    note          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_notify_routes_lookup
    ON notify_routes (enabled, source_key, sort_order);

-- 唯一汇聚点。dedup_key 的唯一索引就是幂等闸门（替代 gold_spread_alerts 里的 _claim_alert）。
-- payload_json 刻意不存图片字节：一张 PNG 的 base64 是几十万字符，会把这一列撑爆。
-- 因此进了重试队列的消息，重试时按无图纯文本降级——字还在，图没了。
CREATE TABLE IF NOT EXISTS notify_outbox (
    id           BIGSERIAL PRIMARY KEY,
    dedup_key    TEXT NOT NULL UNIQUE,
    source_key   TEXT NOT NULL,
    event        TEXT NOT NULL,
    level        TEXT NOT NULL DEFAULT 'info',
    payload_json JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notify_outbox_created
    ON notify_outbox (created_at DESC);

-- 每个 target 一行。「这条消息发出去了没有」只看这里，不看 outbox。
CREATE TABLE IF NOT EXISTS notify_deliveries (
    id              BIGSERIAL PRIMARY KEY,
    outbox_id       BIGINT NOT NULL REFERENCES notify_outbox(id) ON DELETE CASCADE,
    route_id        BIGINT,
    channel         TEXT NOT NULL,
    target_json     JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INT NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT '',
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ,
    UNIQUE (outbox_id, route_id)
);

CREATE INDEX IF NOT EXISTS idx_notify_deliveries_pending
    ON notify_deliveries (status, next_attempt_at)
    WHERE status = 'pending';
