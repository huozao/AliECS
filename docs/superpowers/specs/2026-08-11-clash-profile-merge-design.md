# Clash 配置合成器：自建节点 + 第三方机场订阅（设计）

2026-08-11 定稿。新增页面挂在 admin-ui，新增接口前缀 `/v1/admin/clash-profile`（admin-ui 侧 `API_BASE=/api`，浏览器实际路径是 `/api/v1/admin/clash-profile/*`）。

## 要解决的问题

devbox 的 Clash Verge 里存着两个 profile：一个本地配置（只有自建节点，附完整分流规则与 DNS 策略），一个第三方机场的 remote 订阅（只有节点，规则是机场给的）。用哪边的节点就得切哪个 profile，切换时规则和 DNS 策略跟着变。手机侧同样的问题。

目标：**一份配置同时含两边节点**，客户端里自由选，规则与 DNS 只有一套。

## 关键决策

| 决策 | 内容 | 理由 |
|---|---|---|
| 不做托管订阅端点 | 服务端只生成配置**文件**供下载，不提供长期可访问的订阅 URL | 托管端点意味着国内备案主机上存在一个返回节点信息的公网端点。生成文件没有这个问题 |
| 机场节点走 `proxy-providers` | 客户端 mihomo 自己去机场拉节点，服务端**完全不接触机场** | 定期刷新由 mihomo 按 `interval` 自动完成，比服务端拉取再转发更实时。服务端因此不需要 HTTP 客户端、YAML 解析器、快照表、定时任务、拉取容错 |
| 只输出 Clash YAML | 不做 sing-box JSON、不做 base64 | 客户端只有 Windows Clash Verge 和 Android FlClash，同为 mihomo 内核 |
| 不解析代理协议 | 自建节点定义整段来自环境变量，原样搬进输出 | 以后换协议、加节点都不用改代码 |
| 零新增依赖 | 用现有 fastapi + psycopg + 标准库 `json` | YAML 1.2 是 JSON 超集，`json.dumps` 的输出就是合法 YAML，且转义由标准库保证 |

## 明确不做

- 不提供订阅 URL 端点，因此不需要 token 鉴权、nginx 限频、日志脱敏
- 不做多用户，不做分享
- 不在服务端做节点测速、过滤、重命名——mihomo 的 provider 自带 health-check
- 不引入 subconverter / Sub-Store

## 安全约束（AliECS 是 PUBLIC 仓库）

以下内容**一律不进仓库**，违反即视为泄漏：

| 内容 | 去处 |
|---|---|
| 自建节点完整定义（协议类型、地址、端口、传输与伪装参数、全部凭据、节点名） | 环境变量 `CLASH_SELF_NODES_B64`，值由 SOPS 管理并在部署时渲染 |
| 自建节点服务器地址派生的防回环规则 | 由 `render.py` 从节点定义**运行时推导**，模板文件里不写死 |
| 第三方机场的名称与订阅 URL | 数据库表，admin-ui 里维护 |

仓库里剩下的是：一个通用的"模板 + 节点定义 → YAML"拼接程序，以及一套公开可查的分流规则（GEOSITE/GEOIP/常见域名）。不含任何可用于定位或探测节点的信息。

spec 与代码注释里也不得出现具体 IP、端口、伪装域名、机场名。

## 现状基线

生成的配置以 devbox 现用的本地 profile 为蓝本，它已经包含成熟的：

- `sniffer`（HTTP/TLS/QUIC 嗅探）
- `dns`：fake-ip + `respect-rules: true` + 国内 DoH 作 `nameserver` 与 `proxy-server-nameserver` + 境外 DoH 作 `fallback` + 约 24 条 `nameserver-policy` + `fallback-filter`
- `tun`：`stack: mixed`、`auto-route`、`strict-route`、`dns-hijack`
- `rules`：QUIC 封禁、内网直连、节点自身直连、阿里云直连、AI/GitHub/Google/YouTube/JetBrains 分流、`GEOSITE,CN` + `GEOIP,CN` 兜底直连

这套原样保留。本设计只增加三处改动。

### 暗雷：`节点选择` 组名不可更改

DNS 配置里有约 24 处 `https://1.1.1.1/dns-query#节点选择` 形式的引用，`#` 后面是代理组名。**改名会让境外 DNS 解析全部失效**，而且症状是间歇性的、难排查。

模板与渲染代码中该组名硬编码为 `节点选择`，单测里加断言保护。

## 三处改动

### 改动 1：新增 `proxy-providers`

每个启用的机场生成一个 provider：

```yaml
proxy-providers:
  <provider_key>:
    type: http
    url: <数据库中的订阅 URL>
    interval: 86400
    path: ./providers/<provider_key>.yaml
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
```

`provider_key` 由数据库行 id 生成（`airport1` 形式），不用机场名，避免中文与特殊字符进 YAML key。

mihomo 会解析上游的 `subscription-userinfo` 响应头，因此机场的流量用量与到期时间在 Clash Verge 里照常显示。

### 改动 2：重建 `proxy-groups`

```yaml
proxy-groups:
  - name: "节点选择"                    # 名称不可改，见上文暗雷
    type: select
    proxies: [<自建节点名...>, "自动选择", DIRECT]
    use: [<全部 provider_key>]
  - name: "自动选择"
    type: url-test
    use: [<全部 provider_key>]
    url: https://www.gstatic.com/generate_204
    interval: 300
    tolerance: 50
  - name: "AI服务"
    type: select
    proxies: [<自建节点名...>, "节点选择"]
```

`AI服务` 默认选中第一个自建节点。理由：ChatGPT / Claude 对出口 IP 敏感，机场共享 IP 容易触发风控；自建节点 IP 固定干净。

**边界情况**：一个 provider 都没有（初次部署，或全部禁用）时，`自动选择` 组不生成，`节点选择` 组不带 `use` 字段——空的 url-test 组会让 mihomo 启动失败。

### 改动 3：AI 相关规则改指向

以下规则的目标从 `节点选择` 改为 `AI服务`，其余规则一行不动：

- `challenges.cloudflare.com` 与 `cloudflare.com`
- `openai.com` / `chatgpt.com` / `oaistatic.com` / `oaiusercontent.com` / `auth.openai.com` / `api.openai.com`

## 实现

### 文件清单

| 文件 | 内容 |
|---|---|
| `db/migrations/0049_clash_profile.sql` | 新表 `clash_profile_providers` |
| `services/backend-api/app/clash_profile/template_base.yaml` | 静态段：基础设置、sniffer、dns、tun。原样输出 |
| `services/backend-api/app/clash_profile/template_rules.yaml` | 静态段：规则列表项（**不含 `rules:` 这个 key**，只有 `  - XXX` 行） |
| `services/backend-api/app/clash_profile/render.py` | 纯函数，无 IO |
| `services/backend-api/app/routers/clash_profile.py` | HTTP 层 |
| `services/admin-ui/index.html` | 新增页签 |
| `deploy/ecs/compose.prod.yml` | `backend-api.environment` 加 `CLASH_SELF_NODES_B64`（漏了变量进不了容器） |
| `deploy/ecs/deploy.sh` | heredoc 白名单加同一个键（漏了会被重建过程丢掉） |
| `tests/test_clash_profile_render.py` | 单测 |

模板拆成两个文件而不是一个，是为了让 `render.py` 能在 `rules:` 开头插入运行时推导的防回环规则，而**不需要解析 YAML**。

### 数据表

```sql
CREATE TABLE IF NOT EXISTS clash_profile_providers (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,           -- 显示名，仅 UI 使用
  url         TEXT NOT NULL,           -- 订阅 URL，含 token
  enabled     BOOLEAN NOT NULL DEFAULT TRUE,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

设计成多行而非单行配置：机场跑路是常态，换机场时需要能先加新的、验证通过再删旧的。多行与单行的实现成本几乎相同。

### 渲染逻辑

`render.py` 暴露一个纯函数：

```
render_profile(self_nodes: list[dict], providers: list[dict]) -> str
```

输出按顺序拼接，全程不解析 YAML：

1. `template_base.yaml` 原样
2. `proxies:` + `json.dumps(self_nodes)`
3. `proxy-providers:` + `json.dumps(...)`（无 provider 时整段省略）
4. `proxy-groups:` + `json.dumps(...)`
5. `rules:`
6. 运行时推导的防回环规则
7. `template_rules.yaml` 原样

第 2、3、4 段用 `json.dumps` 生成 flow-style 值，是合法 YAML，且节点名中的中文、引号、emoji 由标准库正确转义。

**防回环规则推导**：遍历 `self_nodes`，取每个节点的 `server` 字段——

- 是 IPv4 地址 → `- IP-CIDR,<addr>/32,DIRECT,no-resolve`
- 是 IPv6 地址 → `- IP-CIDR6,<addr>/128,DIRECT,no-resolve`
- 是域名 → `- DOMAIN,<host>,DIRECT`

机场节点的服务器地址来自 provider、动态且未知，无法预生成同类规则。TUN 模式下 mihomo 依靠 `auto-route` + `auto-detect-interface` 自动绕过自身发出的代理连接，理论上不需要显式规则；此项列入验证清单。

### 环境变量

`CLASH_SELF_NODES_B64`：**base64 编码的** JSON 数组，每个元素是一个完整的 clash proxy 定义。

**为什么是 base64 而不是裸 JSON**（2026-08-11 实施时实测发现，不要"简化"回去）：这个值要穿过四层，每层引号语义都不同——

1. `deploy/ecs/deploy.sh` 用 `set -a; source <env 文件>` 载入。bash 的引号移除会把 `[{"name":"a"}]` 吃成 `[{name:a}]`
2. 同一个脚本用 heredoc 白名单重建 `current.env`，`${VAR}` 展开不会重新加引号
3. 结果文件由 `docker compose --env-file` 按 dotenv 语义读
4. compose 再做 `${VAR}` 插值

裸 JSON 在第 1 层就废了，而且失败形态是部署后 `/download` 报 `json.loads` 错误，本地测不出来。单引号包裹只能救第 1 层，第 2 层展开后又变回裸值。base64 的字符集（`A-Za-z0-9+/=`）穿这四层都不需要任何转义。

代码必须处理缺失场景：未设置、不是合法 base64/UTF-8、解码后不是合法 JSON、空数组、元素缺 `name`/`server`——一律 `/download` 返回 500 并说明原因，不得输出一份没有自建节点的配置，那会让 `AI服务` 组为空导致 mihomo 启动失败。

按 infra 规矩，真实值由 SOPS 管理，`render.sh` 渲染进 business-cn 的 env 文件，示例配置里只放结构占位。

**新增环境变量必须同时改三处，少一处就悄悄失效**：`deploy/ecs/compose.prod.yml` 的 `backend-api.environment`（compose 用显式 `environment:` 而非 `env_file`，没列出的变量根本不会进容器）、`deploy/ecs/deploy.sh` 的 heredoc 白名单（不在名单里的键会被重建过程丢掉）、以及两个 `*.env.example`。

### 接口

全部 `require_admin`，走现有 SSO。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/admin/clash-profile/providers` | 列表 |
| POST | `/v1/admin/clash-profile/providers` | 新增 |
| PUT | `/v1/admin/clash-profile/providers/{id}` | 修改（含启用/禁用） |
| DELETE | `/v1/admin/clash-profile/providers/{id}` | 删除 |
| GET | `/v1/admin/clash-profile/download` | 下载生成的配置，`Content-Disposition: attachment` |
| GET | `/v1/admin/clash-profile/preview` | 返回配置文本，供页面上复制 |

### 页面

admin-ui 新增页签，复用现有 `common/toast.js` 风格：

- 机场列表：名称、URL（默认打码，点击展开）、启用开关、编辑、删除
- 新增机场表单
- 「下载配置」按钮 + 「复制配置文本」按钮
- 一段固定说明：换机场后需要在各客户端重新导入配置；机场自身节点增删由 mihomo 自动跟进，不需要重新导入

## 使用流程

| 场景 | 操作 |
|---|---|
| 机场增删节点、节点改名 | 无需任何操作，mihomo 按 `interval: 86400` 自动同步 |
| 更换机场 | admin-ui 改 URL → 下载配置 → 两台设备重新导入 |
| 自建节点参数变更 | SOPS 改 `CLASH_SELF_NODES_B64` → 重建容器 → 下载配置 → 重新导入 |
| 调整分流规则 | 改 `template_rules.yaml` → PR → 部署 → 下载配置 → 重新导入 |

分流规则从"手工编辑本地 yaml"变成"改仓库文件走 PR"，可追溯性提升，代价是改一次规则要走一轮部署。

## 测试

`tests/test_clash_profile_render.py`，全部针对纯函数，不需要数据库：

| 用例 | 断言 |
|---|---|
| 无 provider | 输出不含 `自动选择` 组；`节点选择` 组不含 `use` 字段 |
| 有 provider | 每个启用的 provider 各生成一段；禁用的不出现 |
| 组名保护 | 输出中存在 `"name": "节点选择"`，防止后续重构改名破坏 DNS |
| 节点 server 为 IPv4 | 生成 `IP-CIDR,.../32,DIRECT,no-resolve` |
| 节点 server 为域名 | 生成 `DOMAIN,...,DIRECT` |
| 节点名含中文与引号 | 输出可被 `json.loads` 解析回来，字段一致 |
| AI 规则指向 | 输出中 openai 相关规则目标为 `AI服务` 而非 `节点选择` |
| 自建节点为空 | 抛出明确异常，不产出配置 |

按 AGENTS.md，运行 `python -m unittest discover -s tests`。

### 手工验证（单测覆盖不到）

1. **配置语法校验**：用 Clash Verge 自带的 mihomo 核心跑 `verge-mihomo -t -f <生成的配置>`。这一步比任何单测都硬，能抓出拼接产生的缩进与结构错误。不进 CI（CI 环境没有 mihomo 二进制），作为部署前必做步骤。
2. **Windows Clash Verge 导入**：确认两类节点都出现在 `节点选择` 组；确认机场流量与到期信息正常显示。
3. **机场节点连通性**：切到一个机场节点访问境外站点。重点验证 TUN 模式下机场节点不会因为缺少防回环规则而失败。
4. **DNS 未回归**：切到机场节点后访问 YouTube 等依赖 `nameserver-policy` 的站点，确认境外 DNS 仍走代理查询。
5. **AI 服务锁定**：确认 ChatGPT 走的是自建节点而非机场节点。
6. **Android FlClash 导入**：确认同一份配置可用；确认 FlClash 自身的 TUN 开关与模板中 `tun.enable: true` 不冲突；首次导入可能需要先手选节点连上才能下载 geodata。

## 已知风险

| 风险 | 说明 |
|---|---|
| geodata 先有鸡先有蛋 | `GEOSITE,CN` 依赖 `geosite.dat`。手机首次导入时若尚无该文件且无法直连下载，需要先手动选节点连上 |
| 防回环规则覆盖不到机场节点 | 见上文，依赖 mihomo TUN 自身机制，列入验证清单 |
| 规则调整需要走部署 | 相比现在直接编辑本地文件变慢。若后续觉得难受，再考虑把规则段做成 DB 可编辑，但那会牺牲可追溯性，本次不做 |
| 配置文件含自建节点凭据 | 下载后的文件与现在 Clash Verge 里存的内容同等敏感，按同样方式保管。若经飞书投递，等于把 UUID 过一遍第三方服务器 |

## 文档闭环

实施完成后需要回查：

- `docs/project-navigation.md` / `docs/project-ai-map.md`：新增功能入口
- `services/backend-api/README.md`：新增环境变量说明
- infra `secrets/README.md`：新增 SOPS 键
- 顶层 `功能地图-人类版.md`：人类叫法到代码位置的对照
- PR 记录 `Nav-Impact: updated`
