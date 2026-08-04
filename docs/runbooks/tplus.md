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
| timeline 页 500 | tz-aware vs naive 比较 | 已修 36b032a；同类改动注意时区 |
| BOM builder 保存报错 | T+ 报错透传（PR#186） | 委外=IsMadeRequest / 虚拟件=IsPhantom；T+ 请求 body 须 `{"request":{}}` |
| BOM builder 读分类/单位 502、worker `openToken已失效`(403 code=50107) | openToken 续期链路（下方） | 迁移/停机把畅捷通消息地址打成「不再发送」，token 6 天后到期（2026-08-04） |

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
