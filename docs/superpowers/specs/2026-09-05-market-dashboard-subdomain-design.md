# 黄金跨市场看板子域名与部署设计

## 结论

把看板的规范域名定为 `market.hydwang.xyz`。

这个名字描述的是“市场数据看板”这一产品边界，不绑定 `MT5｜MetaTrader 5`、
`Dukascopy` 或单一的黄金价差模型。后续增加多合约、来源对账、复盘窗口时不需要改域名。
`gold.hydwang.xyz` 可作为以后跳转别名保留，但首期不同时维护两个入口。

首期页面先放在现有 `public-web` 静态站的 `/market/` 路径，页面契约稳定后再挂独立
`market.hydwang.xyz` 站点入口。这样可以先完成浏览器、数据字段和鉴权验证，不需要先新增
容器、端口或隧道。

## 现场边界

当前 `public-web` 容器绑定 txecs 的 `127.0.0.1:8080`，后端 API 绑定
`127.0.0.1:8000`。现有主站 NGINX 已将 `/api/` 反向代理到后端；后端现在已加入
`/v1/market/snapshot` 只读接口，默认读取服务端环境变量 `MARKET_SNAPSHOT_FILE`
（默认 `/app/market-data/latest.json`）。接口未收到快照时明确返回“暂无快照”，不生成演示行情。

接口只返回白名单字段，`limit` 在服务端限制为 1～2000；浏览器不能通过请求参数选择文件，
也不能读取原始 tick、Parquet 或任意路径。

MT5 原始逐笔文件、Dukascopy `bi5` 文件和 Parquet 不直接暴露给浏览器。浏览器只读经过
字段筛选的聚合快照，并同时携带源时刻、接收时刻、来源状态和对账状态。

## 访问路径

```text
market.hydwang.xyz/
├── /                 实时价格带看板（每个合约一行）
├── /compare/         MT5 与 Dukascopy 的 1 秒对账视图（第二阶段）
├── /replay/          历史事件与复盘窗口（第三阶段）
└── /api/v1/market/   同源只读 API，不提供任意文件读取
    └── snapshot      当前快照，限制 limit 和时间范围
```

## 数据流

```text
Windows MT5 采集器
    ├── 保存原始 tick 与本地审计文件
    └── 通过出站 HTTPS 发布“最新/1 秒聚合快照”
             ↓
txecs 后端 API / 数据库（只读查询）
             ↓
market.hydwang.xyz 页面
```

采集器断线时页面显示 `MT5_OFFLINE｜MT5 离线` 或 `STALE｜数据过期`，不能静默用
Dukascopy 伪装成 MT5 连续数据。若未来启用 Dukascopy 实时补位，必须在每行保留
`source_status｜来源状态`、`source_timestamp｜源时刻` 和 `fallback_reason｜补位原因`。

## API 最小契约

`GET /api/v1/market/snapshot?limit=200`

采集器跨主机发布使用独立的内部端点：

`POST /api/v1/internal/market/snapshot`，请求头为
`X-Market-Snapshot-Token`。该 token 与浏览器用户 Bearer 令牌分离；未配置服务端
`MARKET_SNAPSHOT_INGEST_TOKEN` 时端点保持禁用。接收端先白名单筛选，再以临时文件加
`os.replace` 原子替换 `MARKET_SNAPSHOT_FILE`，不会把请求中的私有字段落盘。

```json
{
  "schema_version": 1,
  "status": "ok",
  "source_timestamp": "2026-09-05T08:00:01.200Z",
  "ingested_at": "2026-09-05T08:00:01.260Z",
  "contract_count": 2,
  "rows": [
    {
      "au_symbol": "SHFE.au2612",
      "source_status": "ok",
      "source_timestamp": "2026-09-05T08:00:01.200Z",
      "ingested_at": "2026-09-05T08:00:01.260Z",
      "au_price_cny_per_g": 812.34,
      "international_cny_per_g": 811.92,
      "spread_cny_per_g": 0.42,
      "xauusd_usd_per_oz": 3542.10,
      "usdcnh": 7.12450,
      "comparison_status": "matched"
    }
  ],
  "comparison": {
    "bucket_seconds": 1,
    "compared_buckets": 1,
    "mismatch_buckets": 0,
    "mt5_only_buckets": 0,
    "dukascopy_only_buckets": 0
  }
}
```

字段的分母必须在后端计算并返回，前端不自行把快照行数当成交笔数。原始成交、tick、
模拟成交和模型标签仍在 gold-spread-monitor 的审计目录中保留，不通过这个看板 API 输出。

## 子域名接入清单（后续一次性变更）

1. 在 txecs 变量源增加 `MARKET_DOMAIN=market.hydwang.xyz`，并在服务清单增加独立
   `market-dashboard` 服务记录；不修改现有 `hydwang.xyz`、`erp.hydwang.xyz` 等服务的
   上游关系。
2. 新增独立 NGINX HTTPS vhost：静态页面走 `public-web`，`/api/` 走
   `127.0.0.1:8000`，只允许同源访问；页面自身通过同源 OIDC 登录拿到短期 Bearer
   令牌，静态页面和快照 API 再沿用现有 Authelia `auth_request` 的 SSO 入口。OIDC 登录
   路径需要在 vhost 中保留回调可达性，避免把登录入口拦成重定向循环。
3. 为 `market.hydwang.xyz` 申请证书，执行 `nginx -t`、配置预检、主站回归和浏览器验证。
4. DNS 首期保持与当前 business edge 一致的 DNS-only 模式；确认稳定后再单独评估
   Cloudflare 代理，不和域名上线绑在同一次变更里。
5. 只有当独立 API 或页面负载确实需要时，才从静态复用升级为新的 `market-web` 容器；
   新容器必须使用未占用的 loopback 端口并登记到端口清单。

## 分阶段交付

| 阶段 | 交付 | 不做的事 |
|---|---|---|
| 0 | 页面与 `/v1/market/snapshot` 字段契约、SSO 访问范围 | 不接生产 DNS |
| 1 | `/market/` 静态页面、无数据/离线状态、移动端表格 | 不伪造行情、不暴露原始文件 |
| 2 | MT5 发布器、后端只读快照 API、MT5/Dukascopy 1 秒对账 | 不把补位数据标成 MT5 |
| 3 | `market.hydwang.xyz` vhost、证书、DNS、监控与回滚 | 不改变现有主站路由 |
| 4 | 多合约交互图；优先评估 TradingView Lightweight Charts | 不在首期引入 Grafana 作为生产依赖 |

## 回滚

首期页面只新增 `/market/` 静态目录，删除或回滚该目录即可恢复。正式子域名上线时，
回滚顺序为：停止新 vhost → 恢复 DNS 记录 → 保留 API 和审计数据；不得删除原始 tick、
对账桶或来源日志。
