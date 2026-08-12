-- 统一同步平台元数据层（设计：docs/superpowers/specs/2026-08-11-unified-sync-center-design.md）
-- 只统一「元数据」：作业登记、运行、步骤、告警。
-- 业务数据仍归各自的表（external_records / tplus_bom_records / tplus_inventory_records），
-- 本迁移不碰它们，也不写入任何数据。
--
-- 与现有两套 run 表的关系：P1 起 worker 在原写入点后「追加」写本层，
-- sync_job_runs.legacy_ref 回指 sync_runs / integration_sync_runs 的原始行，双写期可对账。
-- 旧表不删、旧页面 API 不动。

-- 作业登记：一行 = 一个可调度的作业。pull（拉取）之外也登记 writeback / reconcile，
-- 否则父件核对这类「写出去」的作业永远进不了告警与新鲜度判定。
CREATE TABLE IF NOT EXISTS sync_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    source_id BIGINT REFERENCES external_sources(id) ON DELETE SET NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    schedule JSONB NOT NULL DEFAULT '{}'::jsonb,
    freshness_sla_seconds INTEGER,
    artifact_glob TEXT,
    alert_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    alert_chat_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_provider_enabled
    ON sync_jobs(provider, enabled);

-- 每次执行。error_kind 是分类而非自由文本：页面和告警直接展示「凭据过期」这类短语，
-- 而不是把 traceback 丢给人自己猜。
CREATE TABLE IF NOT EXISTS sync_job_runs (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL DEFAULT 'schedule',
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    row_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT,
    error_message TEXT,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    legacy_ref JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_sync_job_runs_job_started
    ON sync_job_runs(job_id, started_at DESC);

-- 首屏「最后成功时间 / 新鲜度」是热路径，单独给成功行一条偏索引。
CREATE INDEX IF NOT EXISTS idx_sync_job_runs_job_success
    ON sync_job_runs(job_id, finished_at DESC)
    WHERE status = 'success';

-- 步骤：现在完全缺失的一层。没有它，失败只能看到「退出码 1」，
-- 看不出是取 token 失败、分页第 7 页 429、还是写库失败。
CREATE TABLE IF NOT EXISTS sync_job_steps (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES sync_job_runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    items INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

-- 唯一而非普通索引：P1 要按 (run_id, seq) upsert 步骤状态（running → success/failed），
-- 没有冲突键就只能先查后写，并发下会插出重复步骤。
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_job_steps_run_seq
    ON sync_job_steps(run_id, seq);

-- 告警状态机。下面那条 partial unique index 是防刷屏的根：
-- 一个作业一种告警同时只可能有一条 open，P3 的 notifier 靠抢占成功才推送来防重复。
-- 抢占的确定写法（偏索引必须带谓词才能完成 index inference，
-- 写成 ON CONFLICT (job_id, alert_kind) 不带 WHERE 会直接报 42P10）：
--
--   INSERT INTO sync_job_alerts (job_id, alert_kind, payload_json)
--   VALUES ($1, $2, $3)
--   ON CONFLICT (job_id, alert_kind) WHERE state = 'open' DO NOTHING
--   RETURNING id;          -- 返回 0 行 = 抢占失败 = 已有人推过，本次不推
--
-- 不要用无 target 的 ON CONFLICT DO NOTHING：它会吞掉本表**任何**唯一索引的冲突，
-- 今天只有 PK 和这条偏索引所以碰巧安全，日后再加唯一索引就会静默扩大吞噬范围。
CREATE TABLE IF NOT EXISTS sync_job_alerts (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    -- 触发本条告警的那次运行。允许为空：新鲜度类告警（artifact_stale / 长时间没跑）
    -- 本就没有对应的 run。run 被清理时置空而不是连告警一起删。
    run_id BIGINT REFERENCES sync_job_runs(id) ON DELETE SET NULL,
    alert_kind TEXT NOT NULL,
    -- 去重的正确性完全骑在这个字面量上：下面那条偏索引只认 'open'。
    -- 写成 'OPEN' 不会报错，只会静默绕过去重 → 同一告警无限刷屏。故用 CHECK 焊死。
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'resolved')),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_notified_at TIMESTAMPTZ,
    notify_count INTEGER NOT NULL DEFAULT 0,
    resolved_at TIMESTAMPTZ,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_job_alerts_open
    ON sync_job_alerts(job_id, alert_kind)
    WHERE state = 'open';

CREATE INDEX IF NOT EXISTS idx_sync_job_alerts_state_seen
    ON sync_job_alerts(state, first_seen_at DESC);
