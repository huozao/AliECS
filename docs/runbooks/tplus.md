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
- 写入开关唯一持久源 = sops 的 `aliecs.enc.env`（release-meta.env 是 render 产物）。

## 症状表

| 症状 | 先查 | 处置/根因史 |
|---|---|---|
| 配方里冒出 T+ 没有的 BOM | 僵尸记录 SQL（下方） | missing_since 剪枝只在全量模式做；跑一次全量同步自动剪除（PR#136） |
| 数据像被截断/少了 | 各模块是否走 `paginate_query` | 翻页不变量：全量必须翻页取完，不可依赖服务端默认上限（PR#110） |
| 价格好几天没更新 | `/tplus-sync/` 时间线 + 定时开关是否被关 | 价格走 reportQuery 两报表（翻页用 TaskSessionID，停在 PageIndex>=Pages） |
| 关了开关后重启 worker，之后不同步 | — | 已知行为：disabled 后重启会 sleep 一整轮；等下轮或重建 worker |
| timeline 页 500 | tz-aware vs naive 比较 | 已修 36b032a；同类改动注意时区 |
| BOM builder 保存报错 | T+ 报错透传（PR#186） | 委外=IsMadeRequest / 虚拟件=IsPhantom；T+ 请求 body 须 `{"request":{}}` |

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
- 生产 psql：`docker exec -i ecs-postgres-1 psql -U app -d app`。
