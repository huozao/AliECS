# AdventureLog 接管 couple「地图+相册」实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 `superpowers:executing-plans` 或 `superpowers:subagent-driven-development` 逐任务实现。步骤用 `- [ ]` 跟踪。
> 依据 spec：`AliECS/docs/superpowers/specs/2026-06-14-adventurelog-couple-design.md`。

**Goal:** 在旧电脑以独立 compose 栈部署 AdventureLog（旅行足迹 + Immich 相册），经 SSH 反向隧道 + ECS nginx 两子域名对外；迁移现有带坐标回忆进去；自建 couple 保留并互链、旧 `/map/` 退役。

**Architecture:** AdventureLog 三容器(SvelteKit 前端 / Django 后端 / 独立 PostGIS) 跑旧电脑、与 Immich 共存；ECS 只加 nginx 两 server 块 + autossh 隧道 unit；AliECS 侧只改 couple 前端两页 + 一个一次性迁移脚本。

**Tech Stack:** Docker Compose / AdventureLog(Django REST + SvelteKit) / PostGIS / nginx + certbot / autossh / Python(psycopg + httpx) / pytest。

---

## 执行边界（重要）

本计划分两类任务：
- **【CODE】**Codex 可在 git 仓内完成（AliECS 的迁移脚本 + couple 前端；infra 仓的部署物料文件）。走 TDD、逐任务 commit、不 push、单分支 `codex/adventurelog-couple-2026-06-14`。
- **【OPS】**只能在主机/外部后台做（旧电脑跑 compose、填密钥、起隧道、ECS reload nginx、Immich 配 key、建用户、真跑迁移、验收）。Codex **不执行**，只把命令/清单写进 `docs/ops/`，交用户人工执行。

⚠️ infra 仓是否在 Codex 白名单未定；若不在，infra 类文件（compose/nginx/tunnel）就由 Codex 输出到 `AliECS/docs/ops/adventurelog/` 作为交付物，用户手动放进 infra 仓/主机。

红线：不写真实密钥进 git（只写 `.env.example` 占位）；镜像 pin tag/sha；不动 AliECS Postgres 与 Immich 栈。

---

## 文件结构

```
infra(或交付到 AliECS/docs/ops/adventurelog/):
  laptop/adventurelog/docker-compose.yml          # 3容器栈
  laptop/adventurelog/.env.example                # 占位env(两子域名/关注册/db)
  laptop/adventurelog-tunnel.service              # autossh -R 18015/18016
  server/nginx/adventure.hydwang.xyz.conf         # 前端 server 块
  server/nginx/adventure-media.hydwang.xyz.conf   # 后端 server 块
  docs/runbook-adventurelog.md                    # 上线/回退手册
AliECS:
  scripts/adventurelog/migrate_memories.py        # 一次性迁移ETL
  scripts/adventurelog/transform.py               # DB行→adventure payload(纯函数,可测)
  scripts/adventurelog/al_client.py               # AdventureLog REST 薄封装
  tests/test_adventurelog_migration.py            # transform TDD
  services/public-web/couple/index.html           # 地图/相册卡片改外链
  services/public-web/map/index.html              # 退役:提示+跳转
  docs/ops/adventurelog-deploy-2026-06-14.md       # OPS 上线清单
  docs/ops/adventurelog-migration-report-2026-06-14.md  # 迁移对账(脚本产出)
```

---

## Phase A — 部署物料（【CODE】写文件，【OPS】后续应用）

### Task A1: AdventureLog compose 栈文件

**Files:** Create `infra/laptop/adventurelog/docker-compose.yml`

- [ ] **Step 1: 写 compose（pin 版本、独立卷、独立网络）**

```yaml
name: adventurelog
services:
  web:
    image: ghcr.io/seanmorley15/adventurelog-frontend:v0.x.y   # 实施时 pin 到最新稳定 tag, 勿用 latest
    container_name: adventurelog-frontend
    restart: unless-stopped
    env_file: [.env]
    ports: ["127.0.0.1:8015:3000"]     # 仅本机监听, 经隧道暴露
    depends_on: [server]
  server:
    image: ghcr.io/seanmorley15/adventurelog-backend:v0.x.y    # 同上 pin
    container_name: adventurelog-backend
    restart: unless-stopped
    env_file: [.env]
    ports: ["127.0.0.1:8016:80"]
    depends_on: [db]
    mem_limit: 1g                       # 旧电脑与 Immich/webdock 争内存, 设限
  db:
    image: postgis/postgis:16-3.5
    container_name: adventurelog-db
    restart: unless-stopped
    env_file: [.env]
    volumes: ["adventurelog_pgdata:/var/lib/postgresql/data"]
    mem_limit: 768m
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 10
volumes:
  adventurelog_pgdata:
```

- [ ] **Step 2: 校验语法** — Run: `docker compose -f infra/laptop/adventurelog/docker-compose.yml config`（旧电脑或本地有 docker 处）Expected: 正常输出解析后的 compose、无报错。
- [ ] **Step 3: commit** — `git commit -m "feat(adventurelog): compose 栈(pin版本/独立卷/mem限)"`

### Task A2: .env.example（两子域名 + 关注册 + db）

**Files:** Create `infra/laptop/adventurelog/.env.example`

- [ ] **Step 1: 写占位 env**

```bash
# ===== frontend(web) =====
ORIGIN=https://adventure.hydwang.xyz
PUBLIC_SERVER_URL=https://adventure-media.hydwang.xyz
FRONTEND_PORT=8015
BODY_SIZE_LIMIT=104857600            # 100MB; 与 nginx client_max_body_size 对齐
# ===== backend(server) =====
PUBLIC_URL=https://adventure-media.hydwang.xyz
CSRF_TRUSTED_ORIGINS=https://adventure.hydwang.xyz,https://adventure-media.hydwang.xyz
BACKEND_PORT=8016
DEBUG=False
SECRET_KEY=__FILL_STRONG_RANDOM__
DJANGO_ADMIN_USERNAME=__FILL__
DJANGO_ADMIN_PASSWORD=__FILL_STRONG__
DJANGO_ADMIN_EMAIL=__FILL__
# 关公开注册: 实施时核对该版本变量名(如 DISABLE_REGISTRATION=True);
# 若该版本无此 env, 见 OPS 清单"首启后到 admin 关注册"
# ===== db =====
POSTGRES_DB=adventurelog
POSTGRES_USER=adventurelog
POSTGRES_PASSWORD=__FILL_STRONG__
PGHOST=db
```

- [ ] **Step 2: commit** — `git commit -m "feat(adventurelog): .env.example(两子域名/关注册/db占位)"`

### Task A3: 反向隧道 unit（autossh -R 18015/18016）

**Files:** Create `infra/laptop/adventurelog-tunnel.service`

- [ ] **Step 1: 写 systemd unit（参照现有 webdock 隧道风格 + keepalive）**

```ini
[Unit]
Description=AdventureLog reverse SSH tunnel to ECS (18015 frontend, 18016 backend)
After=network-online.target
Wants=network-online.target

[Service]
Restart=always
RestartSec=5
# ServerAliveInterval/CountMax 防掉线占端口(参照 10-tunnel-keepalive.conf 教训)
ExecStart=/usr/bin/ssh -NT \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes -o StreamLocalBindUnlink=yes \
  -R 18015:localhost:8015 -R 18016:localhost:8016 aliecs

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: commit** — `git commit -m "feat(adventurelog): 反向隧道 systemd unit(18015/18016+keepalive)"`

### Task A4: ECS nginx 两 server 块

**Files:** Create `infra/server/nginx/adventure.hydwang.xyz.conf`、`infra/server/nginx/adventure-media.hydwang.xyz.conf`

- [ ] **Step 1: 前端 server 块**

```nginx
# adventure.hydwang.xyz -> 旧电脑前端(经隧道 127.0.0.1:18015)
server {
  listen 443 ssl http2;
  server_name adventure.hydwang.xyz;
  # ssl_certificate / ssl_certificate_key 由 certbot 注入
  client_max_body_size 100m;
  location / {
    proxy_pass http://127.0.0.1:18015;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;       # SvelteKit/WS
    proxy_set_header Connection "upgrade";
  }
}
```

- [ ] **Step 2: 后端/媒体 server 块**（同结构，`server_name adventure-media.hydwang.xyz`，`proxy_pass http://127.0.0.1:18016;`）。
- [ ] **Step 3: commit** — `git commit -m "feat(adventurelog): ECS nginx 两子域 server 块"`

### Task A5: 上线/回退手册

**Files:** Create `infra/docs/runbook-adventurelog.md`（或交付到 `AliECS/docs/ops/adventurelog/`）

- [ ] **Step 1: 写手册**——内容 = 本计划 Phase D 的 OPS 清单全文（部署→密钥→隧道→nginx→TLS→Immich→建用户→迁移→验收→回退）。
- [ ] **Step 2: commit** — `git commit -m "docs(adventurelog): 上线/回退手册"`

---

## Phase B — couple 互链 + 旧 /map/ 退役（【CODE】AliECS public-web）

### Task B1: dashboard 地图/相册卡片改外链

**Files:** Modify `AliECS/services/public-web/couple/index.html`

- [ ] **Step 1: 读现状**——定位 dashboard 里"地图足迹""相册"两个模块入口的现有 DOM（spec §6）。记下选择器/锚点到 Task note。
- [ ] **Step 2: 改为外链**——两卡片的点击改为 `window.open('https://adventure.hydwang.xyz','_blank','noopener')`；文案标注"前往 旅行足迹/相册（AdventureLog）"。保持页面现有样式与鉴权门不变。
- [ ] **Step 3: 验证**——若有 `tests/test_*frontend*.py` 的 DOM 断言约定，加一条"couple 页含指向 adventure.hydwang.xyz 的链接"；否则在 `docs/ops/adventurelog-deploy-2026-06-14.md` 记手动验证项。
- [ ] **Step 4: commit** — `git commit -m "feat(couple): 地图/相册入口外链到 AdventureLog"`

### Task B2: 旧 /map/ 退役为跳转

**Files:** Modify `AliECS/services/public-web/map/index.html`

- [ ] **Step 1: 写失败测试/断言**——`/map/index.html` 应含跳转到 `adventure.hydwang.xyz` 的 meta refresh 或 JS 跳转 + "已迁移"提示文案（若有前端测试约定）。
- [ ] **Step 2: 改页面**——保留路由(防书签404)，body 换成"地图足迹已迁移至 AdventureLog"提示卡 + `setTimeout(()=>location.href='https://adventure.hydwang.xyz', 1500)`。删掉 Leaflet 初始化逻辑（不再需要）。后端 `/v1/map/memories` 端点**暂保留**（迁移脚本要用），迁移完成后另议下线。
- [ ] **Step 3: 验证** — PASS / 手动验证项记 ops 文档。
- [ ] **Step 4: commit** — `git commit -m "feat(map): 旧/map/退役为跳转AdventureLog(保留路由防404)"`

---

## Phase C — 迁移 ETL（【CODE】AliECS scripts，TDD transform + 薄客户端）

### Task C1: transform 纯函数（DB 行 → adventure payload）

**Files:** Create `AliECS/scripts/adventurelog/transform.py`；Test `AliECS/tests/test_adventurelog_migration.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_adventurelog_migration.py
from scripts.adventurelog.transform import memory_to_adventure
def test_memory_to_adventure_basic():
    row = {
        "id": 42, "title": "春分礼物", "content": "在公园…",
        "place_name": "中央公园", "lat": 31.23, "lng": 121.47,
        "memory_date": "2026-03-20", "visibility": "private",
        "tags": ["纪念日", "礼物"],
    }
    a = memory_to_adventure(row)
    assert a["name"] == "春分礼物"
    assert a["latitude"] == 31.23 and a["longitude"] == 121.47
    assert a["location"] == "中央公园"
    assert a["is_public"] is False                 # visibility=private
    assert a["external_ref"] == "aliecs-memory:42" # 幂等键(写进描述或自定义字段)
    assert "春分礼物" not in a["external_ref"]
def test_memory_without_coords_is_skipped():
    from scripts.adventurelog.transform import has_coords
    assert has_coords({"lat": None, "lng": None}) is False
```

- [ ] **Step 2: 跑红** — `PYTHONPATH=. pytest tests/test_adventurelog_migration.py -v` → FAIL。
- [ ] **Step 3: 实现 transform**

```python
# scripts/adventurelog/transform.py
def has_coords(row) -> bool:
    return row.get("lat") is not None and row.get("lng") is not None

def memory_to_adventure(row: dict) -> dict:
    return {
        "name": row["title"],
        "location": row.get("place_name"),
        "latitude": row.get("lat"),
        "longitude": row.get("lng"),
        "visit_date": str(row.get("memory_date")) if row.get("memory_date") else None,
        "description": row.get("content") or "",
        "tags": list(row.get("tags") or []),
        "is_public": row.get("visibility") == "shareable",
        "external_ref": f"aliecs-memory:{row['id']}",   # 幂等键
    }
```

> ⚠️ AdventureLog 真实 adventure 字段名（name/location/latitude/visit_date/is_public…）以**实施时 AdventureLog API 文档为准**核对，对不上就在此函数里改映射；`external_ref` 若 API 无自定义字段，则塞进 description 末尾的 `[ref:aliecs-memory:42]` 标记并据此幂等。

- [ ] **Step 4: 跑绿** → PASS。
- [ ] **Step 5: commit** — `git commit -m "feat(migrate): memory→adventure transform(幂等键)"`

### Task C2: AdventureLog REST 薄客户端 + 迁移驱动（含 dry-run）

**Files:** Create `AliECS/scripts/adventurelog/al_client.py`、`AliECS/scripts/adventurelog/migrate_memories.py`

- [ ] **Step 1: 写失败测试（mock httpx，验证幂等查重 + dry-run 不写）**

```python
def test_migrate_dry_run_creates_nothing(monkeypatch, fake_db_memories, fake_al_api):
    fake_db_memories([{ "id":1,"title":"A","lat":1.0,"lng":2.0, ... }])
    run_migration(dry_run=True)
    assert fake_al_api.created == []           # dry-run 不 POST
def test_migrate_skips_existing_external_ref(fake_db_memories, fake_al_api):
    fake_al_api.preexisting_refs = {"aliecs-memory:1"}
    fake_db_memories([{ "id":1, ... }])
    run_migration(dry_run=False)
    assert fake_al_api.created == []           # 幂等:已存在不重建
```

- [ ] **Step 2: 跑红 → 实现**
  - `al_client.py`：`AdventureLogClient(base_url, token)` 提供 `list_existing_refs()`、`create_adventure(payload)`、`attach_immich_asset(adventure_id, asset_id)`；用 httpx，**超时 + 重试≤3**（与项目外部调用纪律一致）。
  - `migrate_memories.py`：连 AliECS Postgres 读 `memories`(has_coords) → `memory_to_adventure` → 跳过已存在 `external_ref` → `create_adventure`；照片：查 `photos`，`storage_driver` 指向 Immich 的取 asset id 调 `attach_immich_asset`，仅本地的记入对账"需人工"。`--dry-run` 默认 True，`--apply` 才真写。产出 `docs/ops/adventurelog-migration-report-2026-06-14.md`。
- [ ] **Step 3: 跑绿** — `PYTHONPATH=. pytest tests/test_adventurelog_migration.py -v` → PASS。
- [ ] **Step 4: commit** — `git commit -m "feat(migrate): AdventureLog客户端+迁移驱动(dry-run/幂等/对账)"`

> 真实 base_url=`https://adventure-media.hydwang.xyz`、token、Immich asset 关联端点在 **Phase D（OPS）实跑时**对齐；离线 TDD 已覆盖 transform 与幂等/ dry-run 逻辑。

---

## Phase D — 上线清单（【OPS】交用户人工执行，Codex 只输出此清单）

> Codex 把以下写进 `AliECS/docs/ops/adventurelog-deploy-2026-06-14.md`，**不要实际执行**。

- [ ] 1. **旧电脑核内存余量**：`free -h` + 看 Immich/webdock 占用；不足则加 swap 或调 mem_limit。
- [ ] 2. **放部署物料**：把 `infra/laptop/adventurelog/*` 拷到旧电脑；`.env` 由 `.env.example` 复制并填真实密钥（SECRET_KEY/超管/db 口令），**不进 git**。
- [ ] 3. **DNS**：`adventure.hydwang.xyz`、`adventure-media.hydwang.xyz` 解析到 ECS 公网 IP。
- [ ] 4. **起栈**：旧电脑 `docker compose -f docker-compose.yml up -d`；`docker compose logs -f` 看三容器健康。
- [ ] 5. **隧道**：装 `adventurelog-tunnel.service` 到旧电脑、`systemctl enable --now`；ECS `ss -ltnp | grep -E '18015|18016'` 确认两端口在听。
- [ ] 6. **nginx + TLS**：ECS 放两 server 块、`certbot --nginx -d adventure.hydwang.xyz -d adventure-media.hydwang.xyz`、`nginx -t && systemctl reload nginx`；把两 server 块 + 隧道 unit 归档进 infra 仓 runbook（防重建丢失，与 OAuth 路由同类风险）。
- [ ] 7. **关注册**：核该版本注册开关；无 env 则登录 Django admin 关闭公开注册。
- [ ] 8. **建用户**：用 `DJANGO_ADMIN_*` 超管登录，建你俩 2 个账号。
- [ ] 9. **Immich 集成**：在 Immich 各生成个人 API key（asset.read/view、album.read、library.read、user.read）；在各自 AdventureLog 账号设置填 `https://immich.hydwang.xyz/api` + key；测试搜图/挂图成功。
- [ ] 10. **跑迁移**：先 `python -m scripts.adventurelog.migrate_memories`（dry-run）看对账报告；确认无误再 `--apply`；抽样 3-5 条核对地点/坐标/日期；照片"需人工"项逐个补。
- [ ] 11. **验收**（spec §9 清单全过）+ **备份**：AdventureLog PostGIS 卷纳入 restic。
- [ ] 12. **回退预案**：`docker compose down` + 摘 nginx 两块 + 停隧道 unit；自建 couple 不受影响。

---

## 自检（已对 spec 核）

- **Spec 覆盖**：§2 组件→A1/A4/D；§3 env→A2;§4 暴露→A3/A4/D5-6；§5 认证→A2/D7-8；§6 互链/退役→B1/B2；§7 迁移→C1/C2/D10；§8 资源备份→A1(mem_limit)/D1/D11；§9 验收→D11；§10 YAGNI 未引入多余任务；§11 不确定项→在 C1/C2 注释 + D7/D9/D10 实跑核对。无遗漏。
- **占位符**：`.env` 的 `__FILL__`、镜像 `v0.x.y` 是"实施时填真实值"的显式占位（非设计 TBD）；AdventureLog API 字段名不确定处已在 C1 注释明确"以 API 文档为准并据此改映射"，非空泛。
- **一致性**：幂等键全程统一 `aliecs-memory:<id>`/`external_ref`；子域名/端口（adventure→18015、adventure-media→18016）前后一致；`memory_to_adventure`/`has_coords`/`run_migration`/`AdventureLogClient` 命名一致。
