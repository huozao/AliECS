# Runbook：T+ 同步排障（BOM / 存货 / 价格）

## 链路图

```
畅捷通 T+ OpenAPI
  → tplus-sync-worker（txecs 容器 business-cn-tplus-sync-worker-1，只读拉取）
  → Postgres（tplus_bom_records 等，last_seen_at + missing_since 删除追踪）
  → backend-api assemble（WHERE missing_since IS NULL）
  → /formula/ 配方查询、/tplus-sync/ 时间线页
写回方向（BOM builder）：
  backend-api → business-cn-tplus-write-worker-1（独立写消费者）→ T+
```

- 定时同步配置在 DB：`integration_sync_config(provider='chanjet')`，页面 `/tplus-sync/` 顶部可改，worker 每轮热读。
- 调度语义：`interval_seconds` 是周期，`anchor_time` 是执行时刻（**北京时间 HH:MM**，容器内是 UTC）。
  - `anchor_time` 留空 = 跑完睡一个周期，触发时刻逐日漂移（每轮漂几十秒），是旧的默认行为。
  - 设了锚点 = 相位对齐到 `{锚点 + k*周期}`，并且**容器重建后不会在白天补跑全量**——
    worker 启动时从 `integration_sync_runs` 读上次 `scheduled_full` 的时刻判断是否到期。
  - 与 doc-sync 共用同一套语义（`next_scheduled_full_due` / `next_full_sync_due`），两个 worker 行为一致。
  - **睡眠时长也必须按锚点算**（`_seconds_until_next_due`）。PR#267 只在醒来后判断到期、
    却仍睡固定一个周期，结果睡眠期内的锚点被整轮跳过、醒来时刻又成了新相位，锚点永远收敛不了
    （2026-08-05 实测：08-04 18:38 跑完睡 86400 秒，08-05 02:00 那次直接没跑）。
- 每次全量快照有变化就写一条 `integration_reconciliation_diffs` 明细，`status` 只区分
  `needs_review`/`informational`；`/tplus-sync/` 的「详情」因此总能回看本次变化。
  `/health/` 的告警计数只数 `needs_review`，不受 informational 影响。
- 当前 business-cn 写入开关唯一持久源 = infra SOPS 的
  `txecs-production.enc.env`（目标机 runtime env 是渲染产物）。迁移到其他角色时先从
  fleet 和 workflow 确认对应 profile，禁止沿用历史文件名猜测。

## 症状表

| 症状 | 先查 | 处置/根因史 |
|---|---|---|
| 配方里冒出 T+ 没有的 BOM | 僵尸记录 SQL（下方） | missing_since 剪枝只在全量模式做；跑一次全量同步自动剪除（PR#136） |
| 数据像被截断/少了 | 各模块是否走 `paginate_query` | 翻页不变量：全量必须翻页取完，不可依赖服务端默认上限（PR#110） |
| 价格好几天没更新 | `/tplus-sync/` 时间线 + 定时开关是否被关 | 价格走 reportQuery 两报表（翻页用 TaskSessionID，停在 PageIndex>=Pages） |
| 关了开关后重启 worker，之后不同步 | — | 已知行为：disabled 后重启会 sleep 一整轮；等下轮或重建 worker |
| 同步跑在白天 / 时刻逐日漂移 | `/tplus-sync/` 的「执行时刻」是否留空 | 留空=相对间隔会漂；填北京时间 HH:MM 即锚定（如 02:00） |
| 设了执行时刻，那个点却没跑 | `integration_sync_runs` 里最后一条 `scheduled_full` 的 `started_at`，再对 worker 日志的 `sleeping: seconds=` | 睡眠时长没按锚点算就会整轮跳过（已修，见上）。确认修复是否生效：日志里跑完后的 `seconds=` 应是"到下一个锚点"的秒数，不是 86400 |
| timeline 页 500 | tz-aware vs naive 比较 | 已修 36b032a；同类改动注意时区 |
| BOM builder 保存报错 | T+ 报错透传（PR#186） | 委外=IsMadeRequest / 虚拟件=IsPhantom；T+ 请求 body 须 `{"request":{}}` |
| BOM builder 读分类/单位 502、worker `openToken已失效`(403 code=50107) | openToken 续期链路（下方） | 迁移/停机把畅捷通消息地址打成「不再发送」，token 6 天后到期（2026-08-04） |
| 定时全量里只有 bom 模块挂、报 `status=None body=`，其他模块全正常 | worker 日志 `API error detail:` 那行的 message；BOM 的 PageSize | **服务端慢查询撞读超时**。`bom/QueryPage` 每行要展开整棵子件树，耗时随 PageSize 超线性（2026-08-09 实测 219 行：5→2.1s / 20→3.4s / 100→14.6s / **500→38.5s**），而 `REQUEST_TIMEOUT_READ=30`。已拆出 BOM 专用 `TPLUS_BOM_PAGE_SIZE`（默认 50，单页 7~11s）。**不要调大读超时或加重试**——前者数据再长还会撞，后者对确定性超时只是每轮多耗几十秒 |
| `/formula/colors/` 冒出一批「编码失联」，但父件在 T+ 里确实存在 | worker 日志 `Module failed, continuing with the rest: module=inventory`；`integration_sync_runs` 最后一条 `scheduled_full` 的 `detail_json.failed_modules` | 存货档案**只在定时全量里落库**（`job_sync_all` → `persist_inventory_records`）。纯存货父件（无 BOM）全靠它匹配；那一轮没跑成，页面就误判失联（2026-08-07 实测 41 条） |

## 定时全量的失败边界与告警

- **模块独立容错**：`job_sync_all.run()` 每个模块单独 try，一个挂掉只记账、后面照跑。
  失败模块名进 `SyncAllResult.failed_modules` → `integration_sync_runs.detail_json.failed_modules`。
  退出码取第一个失败模块的码（配置错 2 / 接口错 3 / 其他同步错 4 / 未知 1）。
  历史反例：2026-08-07 18:00 BOM 接口超时，拆分前整轮 abort，存货跟着不落库。
- **失败仍不重试**：worker 记完账就睡到下一个锚点（约 24h）。要立刻补，用手动全量或重启 worker。
- **失败详情看 `integration_sync_runs.error_json`**：结构是 `{"modules": [{module, type, message, endpoint, status}]}`。
  `message` 是唯一能看出真因的字段——`status=None body=` 三个字段全空时，
  `read timeout=30` 只在 message 里。2026-08-09 之前 `error_json` 恒为 `{}`，
  BOM 连挂四天都只能看到 `status=None`，最后靠手动复现才定位。同一份详情也写进日志
  `API error detail: module=...`。
- **分页规模是每个模块单独一档**：全局 `DEFAULT_PAGE_SIZE=500` 只适合扁平档案
  （inventory 609 行 2 页共 9s）。重查询接口必须单独压小，否则总量一涨单页就变慢、迟早撞读超时；
  压小后数据增长只增加页数，单页耗时恒定。目前只有 BOM 走独立档（`TPLUS_BOM_PAGE_SIZE`）。
- **告警**：backend `ops.py` 的 `tplus-full-sync-watcher` 线程每小时查一次最后一轮 `scheduled_full`，
  失败、`failed_modules` 非空、或超过 2 天没有成功记录都推飞书；同一轮按 `finished_at` 去重只报一次。
  复用 openToken 告警那组凭据（`OPS_ALERT_FEISHU_*`，回退 `VERSION_DIGEST_FEISHU_*`），无新增密钥。
  开关 `TPLUS_SYNC_ALERT_ENABLED`（默认 1）、间隔 `TPLUS_SYNC_ALERT_INTERVAL_SECONDS`（默认 3600）。

## openToken 续期链路（整条 T+ 的命门）

```
畅捷通开放平台（定时约每 10 分钟 POST appTicket）
  → https://hydwang.xyz/api/v1/webhooks/chanjet
  → backend-api handlers.py:_maybe_refresh_open_token（用 CHANJET_CERTIFICATE 换 openToken）
  → 写共享卷 tplus_sync_requests 的 chanjet_open_token.txt
  → backend-api / sync-worker / write-worker 每次调用现读该文件
```

- **openToken 有效期只有 6 天，全靠上面这条 webhook 续命**；没有任何主动拉取兜底，
  appTicket 只能由畅捷通推送，服务端无法自己申请。
- 畅捷通规则：**消息地址连续 24 小时反馈不正确，就标记异常并「不再发送」任何消息**。
  停机、换服务器、域名入口切换都会触发；一旦被标记，即使链路恢复也不会自动恢复推送。
- token 刷新后**不需要重启任何容器**（三处都是每次调用现读文件）。
- `CHANJET_AUTO_EXCHANGE_OAUTH_CODE` 默认 `false`，OAuth 授权码回调只 spool 不换 token，
  别指望在开放平台点「更新授权」能自愈。

### 排障顺序

1. 看 token 有没有在续：
   `ssh txecs 'sudo docker exec business-cn-backend-api-1 ls -l /app/tplus-sync-requests/chanjet_open_token.txt'`
   —— mtime 超过 6 天就是断了。
2. 看畅捷通还推不推：
   `ssh txecs 'sudo grep "webhooks/chanjet" /var/log/nginx/access.log'`
   —— 正常时每 10 分钟一条 `POST ... 200 20`，源 IP 为阿里云北京段（如 39.105.56.85 / 47.93.212.21）。
3. 确认链路本身通（后端对任何 body 都回 `{"result":"success"}`，无副作用）：
   `curl -X POST -H "Content-Type: application/json" -d '{}' https://hydwang.xyz/api/v1/webhooks/chanjet`
4. **恢复动作在畅捷通开放平台，不在服务器**：开放平台 →「消息配置」，
   若「当前平台消息发送状态」显示 **不再发送**，点红色按钮
   **「重置消息地址状态并发送AppTicket」**。约 1 分钟内 token 文件即刷新。
5. 验证（用新 token 只读探测，无副作用）：
   `ssh txecs 'sudo docker exec -i business-cn-tplus-sync-worker-1 python -'` 管道送脚本，
   调 `/tplus/api/v2/inventoryClass/Query`，正常返回 HTTP 200 + 分类行数。

### 根因史（2026-08-04）

7-25 腾讯云迁移后 `hydwang.xyz` 入口变更，aliecs 上原接 webhook 的 nginx server 块被隔离成
默认拒绝（444），畅捷通推 appTicket 连续打空 24 小时 → 标记异常并停推。最后一次成功续期为
7-26 03:57，6 天后（8-01）token 到期：sync-worker 每日 403、BOM builder
`/v1/tplus/inventory-create-options` 返回 502（`tplus_bom.py` 把 urllib 的 HTTPError 统一包成 502）。
链路本身当时已恢复可达，缺的只是开放平台侧重置。**以后任何涉及公网入口的迁移/停机，
事后都要回查这条 webhook 是否仍在收。**

## 诊断 SQL（phantom BOM）

```sql
SELECT record_key, last_seen_at, missing_since FROM tplus_bom_records WHERE record_key LIKE '%<编码>%';
-- last_seen_at 停在很久以前 + missing_since 为空 = 僵尸
-- record_key 格式：<id>|<parent_code>|<version>|<disabled>
```

## 线上只读探测法（不动部署代码）

脚本 base64 管道进容器：`ssh txecs` →
`sudo docker exec -i business-cn-tplus-sync-worker-1 python -`，复用容器
`config` + `ChanjetClient` + 自动刷新 openToken。aliecs 的同名旧 worker 必须保持停止，
避免双端同步或写入。

## 测试

- worker 测试要 `PYTHONPATH="src;."`（config 在 worker 根不在 src），且 CI 不覆盖 worker——改 worker 必须本地跑。
- 当前生产 psql：`ssh txecs 'sudo docker exec -i business-cn-postgres-1 psql -U app -d app'`；
  只读诊断无需写权限，执行迁移须按 deploy runbook 获授权。
