# T+ 定时同步开关与间隔配置 — 设计

日期：2026-06-25

## 目标

在 `https://hydwang.xyz/tplus-sync/` 页面加：① 定时同步开关；② 可配置同步间隔（小时）。改了立即生效、不重启 worker。

## 现状

- 定时全量同步在 **worker 循环** `run_forever`（`services/tplus-sync-worker/.../jobs/worker_loop.py`）：启动先同步一次 → `sleep(TPLUS_SYNC_INTERVAL_SECONDS)`（默认 86400=24h），间隔在**启动时只读一次**的环境变量。**无开关，改间隔须重启 worker。**
- sleep 期间每 `TPLUS_SYNC_POLL_SECONDS`（默认 30s）轮询**手动 BOM 请求**（DB `integration_sync_requests`）+ 订阅变更请求。
- worker 经 `DATABASE_URL` 连 DB（`db_sync_requests.connect_if_configured`）。后端 `main.py` 也连同一 DB。
- tplus-sync 页是只读时间线，admin 门控（`require_admin`）；ops 端点都用 `require_admin`。

## 设计

### 1. 存储：DB 单行配置表（worker 与后端共用）

迁移 `db/migrations/0017_integration_sync_config.sql`：

```sql
CREATE TABLE IF NOT EXISTS integration_sync_config (
    provider         text PRIMARY KEY,
    enabled          boolean NOT NULL DEFAULT true,
    interval_seconds integer NOT NULL DEFAULT 86400,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       text
);
INSERT INTO integration_sync_config(provider) VALUES ('chanjet')
ON CONFLICT (provider) DO NOTHING;
```

初始 `enabled=true / 86400` → **不改变现有行为**。

### 2. worker（`worker_loop.py`）

- 新增可注入 `read_sync_config()`（默认从 DB 读 `integration_sync_config WHERE provider='chanjet'`），返回 `{enabled, interval_seconds}`；任何异常/无行 → **回退到 env 默认**（`TPLUS_SYNC_INTERVAL_SECONDS`、enabled 视为 true），不阻断。
- `run_forever` 每轮循环开头读配置：
  - `enabled=true` → 跑 `sync_once()` + 记录 run（现有逻辑）。
  - `enabled=false` → 跳过定时全量同步，记日志 `scheduled sync disabled`，**不记 run**。
  - `interval_seconds` 取配置值（**热生效**，下一轮即用新值）。
- sleep + 手动/订阅轮询逻辑**不变** → 关定时器后**手动同步、订阅变更同步照常**（已与用户确认）。
- DB 配置读取函数放 `db_sync_requests.py`（沿用 `connect_if_configured`）：`fetch_sync_config(provider='chanjet') -> dict | None`。

### 3. 后端 API（`main.py`，`require_admin`）

- `GET /v1/ops/tplus/sync-config` → `{enabled, interval_seconds, interval_hours, updated_at, updated_by}`。
- `PUT /v1/ops/tplus/sync-config`，body `{enabled: bool, interval_hours: number}`：
  - 校验 `interval_hours >= 1`（**下限 1h**，防误填打爆 2 核机器）；换算 `interval_seconds = round(interval_hours*3600)`。
  - upsert 行，写 `updated_by = 当前用户`、`updated_at = now()`。
  - 无配置表/行时返回默认值（优雅降级）。

### 4. 前端（`services/public-web/tplus-sync/index.html`）

- adminContent 顶部加一张设置卡：
  - 开关「定时同步」（checkbox）。
  - 数字输入「间隔（小时）」（min=1，step=1）。
  - 「保存」按钮 + 行内状态提示（保存中/已保存/失败）。
- admin 通过门控后 `GET` 配置填充；点保存 `PUT`。保存成功提示「下次循环生效」。

### 5. 校验与降级

- 间隔下限 1h；上限给个合理值（如 168h=7d）防误填超大。
- 后端/worker 对配置缺失/DB 不可用一律回退默认、不报错。

## 测试

- worker：`enabled=false` 时不调用 `sync_once`；`interval_seconds` 取自配置（热生效）；DB 读失败回退默认。注入 `read_sync_config` 测。
- 后端：`GET` 返回配置；`PUT` 更新并校验 `<1h` 拒绝（422/400）；`updated_by` 写入。
- 前端：`test_tplus_sync_frontend.py` 断言开关、间隔输入、保存按钮、`/v1/ops/tplus/sync-config` 调用存在。
- 迁移：CI `migration-dry-run` 覆盖。

## 不做（YAGNI）

- 按模块/按 provider 多套配置、cron 表达式、「立即同步」按钮（用户没要；本页是时间线）、多 provider。
