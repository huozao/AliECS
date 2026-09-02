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
    type: file
    path: ./providers/<provider_key>.yaml
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
```

`provider_key` 由数据库行 id 生成（`airport1` 形式），不用机场名，避免中文与特殊字符进 YAML key。

> **⚠️ 上面这段自 2026-08-15 起是 `type: file`；此前是 `type: http` + `url` + `proxy: DIRECT` + `interval: 86400`。**
> 改的原因不是 http 那套写错了（它当时确实修好了自举死锁），而是机场按源 IP 封了家宽出口，
> 客户端无论怎么配都拉不到。拉取已上移到服务端。下面「`proxy: DIRECT` 不是可选项」整节
> 因此变成**历史记录**，不再是当前实现——新情况见〈改动 1b：拉取上移到服务端〉。
> 保留它是因为"mihomo 拉 provider 会走自己的规则链"这条机制仍然成立，
> 谁要是把 http provider 加回来还会再撞一次。

### 改动 1b：拉取上移到服务端（2026-08-15）

**触发原因**：机场对家宽出口做了源 IP 封禁，ICMP 通但 TCP 全端口丢弃，持续 3 天以上未恢复。同一条宽带上的 webdock2 出口 IP 与本机相同，同样出局——这一点是决定性的，它排除了"换台设备去拉"这条路，不是架构问题而是根本不在另一个 IP 上。

| 出口 | 订阅端点 | 结果 |
|---|---|---|
| 家宽（devbox / webdock2） | 域名与 IP 两个入口都试 | `000`，超时 |
| txecs（腾讯云广州） | 同上 | `200`，44911 字节 |
| aliecs（境外机房） | 同上 | 156 ms 快速拒绝 |

**关键排除项**：走代理拉也不行。HK/JP/US/SG 四个机场节点全部 `000`，而同一节点同一时刻访问 `api.ipify.org` 返回 200 —— 说明不是节点坏了，是机场拒绝一切非国内源。

因此 txecs 是唯一可行的拉取方，而 backend-api 正好跑在 txecs 上。

**调度放在消费端，不在服务端**：服务端不新建调度设施，也不新增部署单元（backend-api 无任何后台任务机制，doc-sync-worker 的调度绑定飞书多维表、语义不符）。节点数据的唯一消费者就是客户端，客户端不来取时服务端拉了也没用。

**本阶段：人工触发。** admin-ui 点「立即拉取」→ 下载节点文件 → 放进客户端 `providers/`。`cli.py` 已经写好并可用，但**本机每日计划任务列入下一阶段**，本阶段不做。

判断依据：真正需要动手的时机是节点指纹变化，而那不是每天发生（机场换域名一年几次）。人工模式下的风险是无预警——机场换域名当天会突然全部连不上，得自己想起来去后台看。下一阶段做自动化就是为了消掉这个"想起来"。

下一阶段的形态（已验证可行，未实施）：

```bash
# 本机计划任务，每日一次；先写临时文件再原子改名，避免半截内容被 mihomo 加载
ssh <server> "docker exec <backend-container> python -m app.clash_profile.cli refresh"
ssh <server> "docker exec <backend-container> python -m app.clash_profile.cli nodes airport1" \
  > providers/airport1.yaml.tmp && mv providers/airport1.yaml.tmp providers/airport1.yaml
```

⚠️ 这条路会让一个无人值守的定时任务持有管理员 SSH 凭据。更干净的做法是在服务器上加一个 SFTP-only 的受限身份（形如既有的 `artifact-drop`：`ChrootDirectory` + `ForceCommand internal-sftp`），2026-08-15 讨论时用户选择先不加。实施前重新评估一次。

**没有公开下载端点**：合成结果只在 SSO 后台内可取。一个公网可达的 token 订阅 URL 在形式上就是对外分发服务，是这次刻意避开的形态。

**指纹只算 `type`/`server`/`port` 三元组，不含节点名**：机场把「剩余流量：59.34 GB」「距离下次重置剩余：25 天」伪装成节点混在 `proxies` 里，名字每天变。实测两次独立拉取指纹完全一致，而 08-13 那份旧快照指纹不同，正确反映了协议、节点域名、端口段三者同时更换的整批换代。

#### `proxy: DIRECT` 不是可选项（2026-08-12 补，首版漏了导致节点数 0）

> **⚠️ 本节自 2026-08-15 起是历史记录，不是当前实现。** 见上面〈改动 1b〉。

**mihomo 拉 provider 时走自己的规则链，不绕开它。** 本地探针实测（配置里只有 `MATCH,g`）：

```
[TCP] dial g (match Match/) mihomo --> 127.0.0.1:28899 error: ...
initial proxy provider probe error: Get "http://127.0.0.1:28899/sub": EOF
```

机场订阅域名解析到境外 IP，不命中 `GEOIP,CN`，一路掉到 `MATCH,节点选择` ——「拉订阅」这件事本身要先有可用代理，形成自举依赖，首次导入必然失败。

**且只能直连，不能改走自建节点**，三点实测：

| 出口 | 结果 |
|---|---|
| 国内直连（txecs） | 200 |
| 境外自建节点所在机房（aliecs） | 156ms 快速拒绝 |
| 国内家宽直连（devbox） | 200（后因短时间密集拉取被源 IP 拦截，见「已知风险」） |

机场拒绝境外机房 IP，所以把拉取走代理反而是确定失败的路径。`DIRECT` 也正是 Clash Verge 现有 remote profile 的既有行为——它默认不带 `self_proxy`，同样直连拉取，周期同为 24 小时。

`path` 指向的缓存副本提供了容错：实测把 URL 指向死端口重启，缓存里的节点照常加载且不报 error。**只要首次导入成功一次，之后偶发拉取失败不会清空节点。**

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

> ⚠️ 上面这段 `proxy-groups` 与下面「改动 3」的域名清单，**自 2026-09-02 起只反映首版设计，不再是当前产物的样子**。本文是设计时的时间点材料，保留原样以便对照，当前事实源是 `app/clash_profile/render.py` 与两个模板文件，行为由 `tests/test_clash_profile_render.py` 断言。三处已知偏差：
>
> - `AI服务` 的应急项 2026-08-15 起是**具体机场节点**（`use: [<provider_key>]`），不再是 `"节点选择"` 组——旧写法在自建节点真挂掉时切过去出口不变，等于没切。
> - 组从三个增加到六个：新增 `Dukascopy`（结构性不含自建节点，隔离批量抓数据的出口 IP 与计费流量）、`GitHub`（默认自建节点，git 长连接要稳）、`全球直连`（Windows Update / svchost 指向它而不是内置 DIRECT，留一个面板开关）。
> - AI 域名清单已扩到覆盖 Claude 全域（含 `claude.com` 这个**另一个根域**）、Gemini、grok/x.ai、Copilot、Cursor、Perplexity；每个域名都必须同时出现在规则、`nameserver-policy`、`fallback-filter` 三处。

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
| `db/migrations/0051_clash_profile_snapshots.sql` | 新表 `clash_profile_snapshots`（2026-08-15，服务端拉取结果） |
| `services/backend-api/app/clash_profile/template_base.yaml` | 静态段：基础设置、sniffer、dns、tun。原样输出 |
| `services/backend-api/app/clash_profile/template_rules.yaml` | 静态段：规则列表项（**不含 `rules:` 这个 key**，只有 `  - XXX` 行） |
| `services/backend-api/app/clash_profile/render.py` | 纯函数，无 IO |
| `services/backend-api/app/clash_profile/fetch.py` | 拉机场订阅、算指纹。stdlib only（backend-api 没有 requests/httpx/PyYAML） |
| `services/backend-api/app/clash_profile/store.py` | 快照读写。两个调用方：HTTP 路由与 cli |
| `services/backend-api/app/clash_profile/cli.py` | 给本机每日定时任务用，走 `docker exec`，绕开 SSO |
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
| GET | `/v1/admin/clash-profile/snapshots` | 各订阅源的拉取状态（节点数、指纹、上次成功、最后变化、错误）。**不含节点正文** |
| POST | `/v1/admin/clash-profile/providers/{id}/fetch` | 立即拉取。失败返回 502（失败方是上游机场，不是本服务） |
| GET | `/v1/admin/clash-profile/nodes/{id}` | 下载节点文件，落到客户端 `providers/airportN.yaml` |

**没有免鉴权的下载端点，这是刻意的。** 一个公网可取的 token 订阅 URL 在形式上就是对外分发订阅服务；无人值守的每日同步任务改走 SSH + `docker exec` 调 `cli.py`，不扩大任何访问面。

### 页面

admin-ui 新增页签，复用现有 `common/toast.js` 风格：

- 机场列表：名称、URL（默认打码，点击展开）、启用开关、编辑、删除
- 每行显示快照状态：节点数、指纹前 12 位、上次成功拉取时间、**节点最后一次变化时间**、失败原因
- 每行两个新按钮：「立即拉取」「下载节点文件」
- 新增机场表单
- 「下载配置」按钮 + 「复制配置文本」按钮
- 一段固定说明：**只有节点指纹变化时才需要重新导入**；流量数字每天变动不算变更

显示「最后一次变化」而不是「上次拉取」是有意的：后者每天都在动，没有信息量，用户看久了会忽略；前者才对应"需要动手"这个动作。

## 使用流程

| 场景 | 操作 |
|---|---|
| 机场增删节点、节点改名 | 本阶段需人工：admin-ui 点「立即拉取」→「下载节点文件」→ 覆盖 `providers/airportN.yaml`。覆盖后 mihomo 约 2 秒自动重载，**不用重启内核、不用重新导入配置**（2026-08-15 实测，日志 `[Provider] xxx's content update`）。下一阶段由计划任务代劳 |
| 机场换域名/换协议（节点指纹变化） | admin-ui 会显示「节点最后一次变化」时间 → 下载配置 + 节点文件 → 各客户端重新导入 |
| 更换机场 | admin-ui 改 URL → 点「立即拉取」→ 下载配置 → 两台设备重新导入 |
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
| provider 形态 | 必须是 `type: file`，且产物里不得出现 `url` / `proxy` / 订阅 token |
| Claude 三处齐全 | `anthropic.com` / `claude.ai` / `claudeusercontent.com` 同时出现在规则、`nameserver-policy`、`fallback-filter` |
| AI 类 DNS 同组 | 这些域名的 DoH 后缀是 `#AI服务`，不是 `#节点选择` |
| cloudflare 范围 | 只有 `challenges.cloudflare.com` 指向 `AI服务`，整个后缀不得指过去 |
| AI服务 应急项 | `proxies[0]` 是自建节点，`use` 指向机场 provider，且**不含**「节点选择」组 |

`tests/test_clash_profile_fetch.py` 另外覆盖拉取与指纹（不需要网络，`urlopen` 打桩）：

| 用例 | 断言 |
|---|---|
| 伪节点名变动 | 指纹不变（否则后台天天误报节点已变更） |
| 协议/域名/端口换代 | 指纹改变 |
| `proxy-groups` 不计入 | 节点数只统计 `proxies` 段 |
| 非 Clash 格式响应 | 明确抛错，不静默存成 0 节点 |

按 AGENTS.md，运行 `python -m unittest discover -s tests`。

### 手工验证（单测覆盖不到）

1. **配置语法校验**：用 Clash Verge 自带的 mihomo 核心跑 `clash-meta -t -d <目录> -f <生成的配置>`（目录里要放 `geosite.dat`/`geoip.dat`/`Country.mmdb`，否则只会因缺 geodata 报错）。能抓出拼接产生的缩进与结构错误。不进 CI（CI 环境没有 mihomo 二进制），作为部署前必做步骤。

   ⚠️ **`-t` 不覆盖节点是否真的加载**。它只做静态校验——首版就是这样漏掉 `proxy: DIRECT` 的：`-t` 报 `test is successful`，实跑 provider 节点数 0。必须**真跑一次实例**再查：

   ```bash
   # 换端口、关 tun、加 external-controller，避免干扰正在使用的 Clash Verge
   clash-meta -d <目录> -f <改造后的配置>
   curl -s http://127.0.0.1:29091/providers/proxies    # 节点数与 vehicleType
   curl -s http://127.0.0.1:29091/proxies/AI服务       # now 与可选项，URL 里组名要 percent-encode
   ```

   ⚠️ **每次都要从渲染函数重新生成配置，不要在上一轮的产物上打补丁**。改端口/关 tun 的脚本重复执行会叠加出重复 YAML key，mihomo 直接 `fatal: mapping key already defined`，看起来像"新配置有问题"，实际是测试脚本自己造成的——2026-08-15 踩过一次，差点把结论记错。

   ⚠️ **节点文件缺失不会让内核起不来**（2026-08-15 实测）。mihomo 照常启动，该 provider 静默变成 0 节点，组里只剩自建节点。失败形态是"机场节点凭空消失"而非"启动失败"，排查时容易找错方向。

2. **`file` provider 热重载**：覆盖 `providers/airportN.yaml` 后约 2 秒自动生效，内核不重启，日志出现 `[Provider] xxx's content update`。强制刷新的备用手段是 `PUT /providers/proxies/<name>`（返回 204）。这是每日同步任务成立的前提，改动 provider 形态后必须重测。
3. **Windows Clash Verge 导入**：确认两类节点都出现在 `节点选择` 组；确认机场流量与到期信息正常显示。
4. **机场节点连通性**：切到一个机场节点访问境外站点。重点验证 TUN 模式下机场节点不会因为缺少防回环规则而失败。
5. **DNS 未回归**：切到机场节点后访问 YouTube 等依赖 `nameserver-policy` 的站点，确认境外 DNS 仍走代理查询。
6. **AI 服务四端出口**：确认 ChatGPT 与 Claude 在**网页、桌面版、手机版、终端 CLI** 四处都走自建节点。终端那条最容易漏——Claude Code 不认系统代理，靠 `tun.enable: true` 接管，Windows 上需要 Clash Verge 开服务模式。验法是访问 `api.anthropic.com` 后在内核连接列表里看它落在哪个 chain 上，不要靠推理。
7. **Android FlClash 导入**：确认同一份配置可用；确认 FlClash 自身的 TUN 开关与模板中 `tun.enable: true` 不冲突；首次导入可能需要先手选节点连上才能下载 geodata。手机端没有每日同步任务，节点文件要手动放或改用自包含产物。

## 已知风险

| 风险 | 说明 |
|---|---|
| geodata 先有鸡先有蛋 | `GEOSITE,CN` 依赖 `geosite.dat`。手机首次导入时若尚无该文件且无法直连下载，需要先手动选节点连上 |
| 机场按源 IP 拦截 | 2026-08-12 实测：同一条成都电信家宽在密集拉取（约 5 分钟内 7 次）后被静默丢包，同刻 txecs 仍 200。traceroute 显示包正常出境（香港 PCCW 191ms）后才断，说明是目的端按源 IP 丢弃，不是 GFW 边界拦截。**⚠️ 原文写「持续 30 分钟以上未恢复」，该表述自 2026-08-15 起确认严重低估——实际到 08-15 仍未解封，已超过 72 小时；且机场另一个国内直连入口（IP + 高位端口形式，绕开域名）同样不通，ICMP 3/3 通而 TCP 全端口不通。当时按"临时限速"判断是错的，这是持久封禁。** 正是这条导致拉取整体上移到服务端 |
| 拉取上移后仍受机场策略影响 | 服务端拉取一天一次，远低于触发阈值；但机场若改判定，txecs 出口同样可能进黑名单。届时没有第二条可行出口（aliecs 境外被拒、家宽已封），只能走机场后台申诉。**不要因为排障就在服务端反复手动点「立即拉取」** |
| 客户端不再有兜底缓存 | `type: http` 时代 `path` 缓存能在拉取失败时兜底。现在客户端只有本地文件，文件本身就是"缓存"；同步任务失败时文件保持不动，行为等价。但**如果同步任务写了个坏文件**（例如半截内容），mihomo 会加载出错误的节点集。同步脚本必须先写临时文件再原子改名 |
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
