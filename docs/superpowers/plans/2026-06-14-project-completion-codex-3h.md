# 项目收尾 · Codex 3h 不停跑 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 `superpowers:executing-plans`（本会话批量执行）或 `superpowers:subagent-driven-development`（逐任务派子代理）实现本计划。所有步骤用 `- [ ]` 复选框跟踪。**按 Phase 顺序从上往下做，不要跳号**——顺序已按"无人值守可独立完成度"排好，前面的相纯本地确定性、后面的才碰实时外部。

**Goal:** 在一个分支上一口气补完项目 7 块未完成功能（审计分页、couple 图片、T+ 价格、企微A 控制面板、飞书一对一、加微信入口、企微B 群历史可行性），尽量多地达到"有测试、可验证、可部署"。

**Architecture:** 三仓协作——`AliECS`(backend-api / tplus-sync-worker / doc-sync-worker / admin-ui / public-web / deploy compose)、`webdock`(FastAPI 浏览器中继 / lane 路由)、ECS 主机(OpenClaw 网关配置 / nginx)。控制面板采用**混合真相源**：企微A 智能表人工可编辑 → doc-sync 拉表 upsert 进 Postgres → Postgres 驱动运行时（backend 生成路由/权限，webdock 拉取消费）。

**Tech Stack:** Python 3.12 / FastAPI / psycopg(Postgres 16) / pytest / openpyxl / OpenClaw(node) / Docker Compose / 畅捷通 T+ OpenAPI / 飞书 & 企微 OpenAPI。

---

## 全局执行规则（来自用户拍板，Codex 必须遵守）

1. **分支**：开始即在 `AliECS` 与 `webdock` 各建并切到 `codex/project-completion-2026-06-14`。**全程攒在这一个分支**，逐任务 commit，**不自动合并、不开 PR**（等人工 review）。
2. **提交粒度**：每个 Task 末尾 commit 一次，message 用 `feat(<scope>): …` / `fix(<scope>): …` / `test(<scope>): …`。**直推前不推**——本计划只在本地分支提交，不 push（用户另行决定）。
3. **TDD**：先写失败测试 → 跑红 → 最小实现 → 跑绿 → commit。每个 Phase 结束跑该 Phase 全部测试 + 不破坏既有测试。
4. **实时外部调用：允许**。需要实时凭据/连外网的步骤（T+ 实拉、飞书 websocket、企微回调）**可以真触发**，但必须：① 包 `try/except`+超时+重试上限(≤3)，外部失败时**记录并继续到下一个不依赖它的 Task，不要卡死或无限重试**；② 所有外部交互**先用 fixtures 把解析/转换/落库 TDD 做完**，再单列一个"实时验证 Task"。这样即便外部当时不可用，代码与单测仍是完整可交付的。
5. **不碰的红线**：不改密钥/SOPS 明文、不改 CI/release 配置、不动生产 `.env`；OpenClaw 主机配置改动**先备份**（`cp openclaw.json openclaw.json.bak-<ts>`）。webdock「人工登录→自动化接管」流程不得擅改（见 `webdock-manual-login-then-automation` 红线）。
6. **跑测试的方式**：
   - backend-api / couple / exports / 通用：仓库根 `pytest tests/test_xxx.py -v`。
   - tplus-sync-worker：`PYTHONPATH=services/tplus-sync-worker/src pytest tests/test_tplus_price.py -v`（沿用现有 `tests/` 放置约定；若现有 tplus 测试用别的 PYTHONPATH，跟随之）。
   - webdock：`cd webdock && pytest -v`（见 `webdock/pytest.ini`）。
   - 跑前先建基线：见 Phase 0。
7. **ECS/infra 类改动**（compose 卷、nginx、OpenClaw 配置）：能在 git 仓内改的（如 `AliECS/deploy/ecs/compose.prod.yml`）就改进代码；**只能在主机手动做的**（飞书开放平台后台、企微表结构、扫码、重启容器）一律写成「⚠️ 人工/ops 步骤」清单交给用户，不要假装自动完成。

---

## 文件结构总览（本计划将创建/修改）

```
AliECS/
  services/backend-api/app/main.py                 # ④分页参数  ⑤价格导出catalog  ②路由/权限API  ⑥uploads静态+持久
  services/admin-ui/index.html                     # ④审计折叠+分页UI
  services/public-web/health/index.html            # ③加微信入口  ⑤价格导出tab(自动出现)
  services/tplus-sync-worker/
    config/endpoints.py                            # ⑤价格端点(去pending)
    src/tplus_datahub/modules/purchase_price/*.py  # ⑤实现(占位→真)
    src/tplus_datahub/modules/sales_price/*.py     # ⑤实现(占位→真)
  services/doc-sync-worker/app/…                    # ②读企微A智能表worksheets→upsert DB
  db/migrations/                                    # ②新表  ④(可选索引)
  deploy/ecs/compose.prod.yml                       # ⑥uploads命名卷  ②doc-sync新env
  tests/                                            # 各Phase新增 test_*.py
  docs/ops/                                         # ⑦可行性报告  ②表结构说明
webdock/
  src/browser/lane_routing.py                       # ②从DB派生的路由(拉取)  ①飞书peer隔离
  src/api/routes_*.py                               # ③QR/状态端点(若放webdock)  ②路由拉取
  tests/
ECS主机(人工/ops, 不在git):
  /root/.openclaw/openclaw.json                      # ①飞书事件订阅校验  ③weixin登录态
  nginx, 企微A表, 飞书开放平台后台                      # 人工
```

---

## Phase 0 — 准备与基线（~10 min）

**目标**：建分支、装依赖、跑通现有测试当基线，确认起点是绿的。

- [ ] **Step 1: 建分支**

```bash
cd AliECS && git checkout -b codex/project-completion-2026-06-14
cd ../webdock && git checkout -b codex/project-completion-2026-06-14
```

- [ ] **Step 2: 跑既有测试基线（AliECS）**

Run: `cd AliECS && pip install -q -r services/backend-api/requirements.txt openpyxl && pytest tests/ -q`
Expected: 收集到既有用例并通过（若个别用例需 DB/外部而本地缺，记录跳过项，**不要去改既有用例**）。把通过数记进 commit note。

- [ ] **Step 3: 跑 webdock 基线**

Run: `cd webdock && pip install -q -r requirements.txt && pytest -q`
Expected: PASS。

- [ ] **Step 4: 记录基线**（commit 一个空改动的说明文件）

```bash
# 在 AliECS/docs/ops/ 写 codex-run-2026-06-14-baseline.md，记录两仓基线通过数与跳过项
git add docs/ops/codex-run-2026-06-14-baseline.md && git commit -m "chore(codex): 记录3h收尾任务基线"
```

---

## Phase 1 — ④ 审计日志：分页 + 折叠（~25 min，纯本地可全做完）

**现状**：`backend-api/app/main.py:3823 /v1/admin/audit-logs` 写死 `ORDER BY id DESC LIMIT 200` 无分页；`admin-ui/index.html:683` 一次性 `api('/v1/admin/audit-logs')` 全渲染。

**Files:**
- Modify: `AliECS/services/backend-api/app/main.py:3823-3850`
- Modify: `AliECS/services/admin-ui/index.html`（`<h2>审计日志</h2>` 区块 ~444 行 + 其 render JS ~683 行）
- Test: `AliECS/tests/test_backend_audit_pagination.py`（新建）

- [ ] **Step 1: 写失败测试**——后端分页契约

```python
# tests/test_backend_audit_pagination.py
# 用既有 test_backend_*.py 里的 TestClient/夹具风格(读 test_backend_smoke.py 看如何起 app 与造 admin token、如何 mock _conn)
def test_audit_logs_paginated(admin_client, seed_audit_rows):
    seed_audit_rows(250)  # 造 250 行
    r = admin_client.get("/v1/admin/audit-logs?page=1&page_size=50")
    body = r.json()
    assert r.status_code == 200
    assert len(body["items"]) == 50
    assert body["total"] == 250
    assert body["page"] == 1 and body["page_size"] == 50
    # 第2页不与第1页重叠
    ids1 = {i["id"] for i in body["items"]}
    ids2 = {i["id"] for i in admin_client.get("/v1/admin/audit-logs?page=2&page_size=50").json()["items"]}
    assert ids1.isdisjoint(ids2)
```

> 先读 `tests/test_backend_smoke.py` 与 `tests/test_backend_session_revocation.py`，照搬它们构造 admin 客户端、打桩 `_conn()`/Postgres 的方式，写出 `admin_client` / `seed_audit_rows` 夹具（放本文件或 `tests/conftest.py`，跟随现有约定）。

- [ ] **Step 2: 跑红** — `pytest tests/test_backend_audit_pagination.py -v` → FAIL（当前无 page/total）。

- [ ] **Step 3: 后端实现分页**——改 `admin_audit_logs`

```python
@app.get("/v1/admin/audit-logs")
def admin_audit_logs(
    page: int = 1,
    page_size: int = 50,
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    offset = (page - 1) * page_size
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_logs")
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT id, actor_username, action, target_type, target_id, detail, created_at
                FROM audit_logs ORDER BY id DESC LIMIT %s OFFSET %s
                """,
                (page_size, offset),
            )
            rows = cur.fetchall()
    return {
        "items": [
            {"id": r[0], "actor_username": r[1], "action": r[2], "target_type": r[3],
             "target_id": r[4], "detail": r[5], "created_at": str(r[6])}
            for r in rows
        ],
        "total": total, "page": page, "page_size": page_size,
    }
```

- [ ] **Step 4: 跑绿** — `pytest tests/test_backend_audit_pagination.py -v` → PASS。并跑 `pytest tests/test_backend_smoke.py -q` 确认没破坏既有。

- [ ] **Step 5: 前端折叠+分页 UI**——`admin-ui/index.html`
  - 审计日志区块默认**折叠**（`<details>` 包裹标题，或一个"展开/收起"按钮 toggle `hidden`）。
  - 渲染改为分页：底部"上一页/下一页 + 第 X/共 N 页"，调 `api('/v1/admin/audit-logs?page='+page+'&page_size=50')`，用返回的 `total/page/page_size` 算总页数。
  - 读现有 `admin-ui/index.html` 里其它分页/列表的写法（若有）保持风格；无则用最简 vanilla JS（项目无框架）。

- [ ] **Step 6: 前端验证**——若有 `tests/test_*frontend*.py`（如 `test_formula_frontend.py`）的快照/DOM 测试约定，加一个最小断言确认折叠容器与分页按钮存在；否则在 `docs/ops/` 记一条手动验证清单（登录 admin → 审计日志默认收起 → 展开后能翻页）。

- [ ] **Step 7: commit**

```bash
git add services/backend-api/app/main.py services/admin-ui/index.html tests/test_backend_audit_pagination.py
git commit -m "feat(admin): 审计日志分页+默认折叠"
```

**完成判据**：`/v1/admin/audit-logs?page=&page_size=` 返回 `items/total/page/page_size`；admin 页审计日志默认折叠、可翻页。

---

## Phase 2 — ⑥ couple 图片存储双保险（~35 min，纯本地可全做完）

**根因**：本地存储驱动 `LocalPhotoStorage.base_dir = /tmp/aliecs-uploads`（`main.py:691`，**容器 /tmp，重启即丢**），URL 为 `/uploads/{filename}`（`main.py:574`），但该目录未挂持久卷、且需确认 `/uploads/` 有静态服务路由 → `hydwang.xyz/uploads/<hash>.jpeg` 读不到。用户决策：**两者都做**——本地改持久卷 + 修静态服务，并让 webdock 远端存储为主存兜底。

**Files:**
- Modify: `AliECS/services/backend-api/app/main.py`（`LocalPhotoStorage` ~660-700、静态挂载处、`STORAGE_DRIVER` 选择逻辑）
- Modify: `AliECS/deploy/ecs/compose.prod.yml`（加 `uploads` 命名卷 + 挂到 backend-api `/app/uploads`）
- Test: `AliECS/tests/test_couple_local_photo_storage.py`（新建）；参考既有 `tests/test_couple_webdock_photo_storage.py`、`tests/test_couple_oss_photo_storage.py`

- [ ] **Step 1: 读现状**——读 `main.py` 中 `LocalPhotoStorage`、`STORAGE_DRIVER` 分支、是否有 `app.mount("/uploads", StaticFiles(...))` 或 `@app.get("/uploads/{name}")`。把发现写进 Task note（决定是缺静态路由还是缺持久化，或两者）。

- [ ] **Step 2: 写失败测试**——本地存储持久路径 + 取回

```python
# tests/test_couple_local_photo_storage.py（仿 test_couple_webdock_photo_storage.py）
def test_local_storage_persists_under_configured_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.main import LocalPhotoStorage           # 按现有 import 路径调整
    store = LocalPhotoStorage()
    url = store.save(filename="x.jpeg", content=b"\xff\xd8\xff...", mime="image/jpeg")  # 用真实 jpeg 魔数
    assert url.startswith("/uploads/")
    saved = (tmp_path / "uploads") / url.split("/uploads/")[1]
    assert saved.exists() and saved.read_bytes()  # 落盘
def test_local_storage_serves_saved_file(admin_client, tmp_path, monkeypatch):
    # 上传后 GET /uploads/<name> 应 200 且 content-type=image/jpeg
    ...
```

- [ ] **Step 3: 跑红** → FAIL（取回路由缺失/路径不符）。

- [ ] **Step 4: 实现**
  - **持久目录**：保留 `LOCAL_UPLOAD_DIR` env，默认从 `/tmp/aliecs-uploads` 改为 `/app/uploads`（容器内持久卷挂载点）。
  - **静态服务**：若缺路由，加显式取回端点（可控、便于鉴权/Content-Type）：

```python
from fastapi.responses import FileResponse
@app.get("/uploads/{name}")
def serve_upload(name: str) -> FileResponse:
    base = Path(os.getenv("LOCAL_UPLOAD_DIR", "/app/uploads")).resolve()
    target = (base / name).resolve()
    if base not in target.parents or not target.is_file():   # 防目录穿越
        raise HTTPException(404, "not found")
    return FileResponse(target)
```

  - **webdock 主存兜底**：当 `STORAGE_DRIVER=webdock` 时优先走 webdock（`WEBDOCK_PHOTO_BASE_URL`，见 compose:38），webdock 不可达时回退本地卷。把这段"主+兜底"逻辑封在存储选择函数里，加一条单测覆盖回退分支（mock webdock 抛错→断言落到本地）。

- [ ] **Step 5: compose 加持久卷**——`deploy/ecs/compose.prod.yml`

```yaml
  backend-api:
    # …existing…
    volumes:
      - tplus_sync_output:/app/tplus-output:ro
      - recipe_active_bom:/app/recipe-active-bom
      - tplus_sync_requests:/app/tplus-sync-requests
      - uploads:/app/uploads          # 新增：couple 本地图片持久化
    environment:
      LOCAL_UPLOAD_DIR: ${LOCAL_UPLOAD_DIR:-/app/uploads}
# volumes: 段末尾加
volumes:
  # …existing…
  uploads:
```

- [ ] **Step 6: 跑绿** — `pytest tests/test_couple_local_photo_storage.py tests/test_couple_webdock_photo_storage.py -v` → PASS。

- [ ] **Step 7: 巡检 couple 其它端点**——"其他所有功能"排查（用户item⑥后半句）。grep `main.py` 里 couple/memories/photos 相关路由（`/v1/photos*`、`/v1/couple*`、`/v1/memories*`），逐个核对：是否同样依赖易失 `/tmp`、是否有取回路由、删除是否清盘。把发现 + 修复（若小）记进 `docs/ops/couple-audit-2026-06-14.md`；较大的另起 TODO，不在本 Phase 强行做完。

- [ ] **Step 8: commit**

```bash
git add services/backend-api/app/main.py deploy/ecs/compose.prod.yml tests/test_couple_local_photo_storage.py docs/ops/couple-audit-2026-06-14.md
git commit -m "fix(couple): 本地图片持久卷+取回路由, webdock主存兜底, 修/uploads读不到"
```

- [ ] **⚠️ ops 步骤（交用户）**：部署后该卷为空，旧 `/tmp` 里的历史图已随重启丢失，无法找回；如有重要历史图需用户从源重新上传。compose 改动需走部署生效。

**完成判据**：单测覆盖"落盘+取回+webdock 回退"；`/uploads/<name>` 200；compose 有 `uploads` 命名卷。

---

## Phase 3 — ⑤ T+ 采购/销售价格 同步+导出（~50 min，fixtures 全做完 + 实时验证）

**现状**：`config/endpoints.py:47-48` `purchase_price/sales_price = "pending"`；`modules/purchase_price/*`、`modules/sales_price/*` 是 `raise_pending` 占位；导出目录 `/v1/exports/catalog`（`main.py:1721`）已存在、`/health/` 数据导出 UI 自动渲染 tab。已有研究（见记忆 `tplus-purchase-price-research`）：**价格在 `GetVoucherDTO` 的 `data.Details[]`**——`OrigTaxPrice`(含税单价)/`OrigDiscountPrice`(无税单价) 实测有值；`FindVoucherList` 明细字段被静默忽略，须先列 voucher 再逐单 `GetVoucherDTO`。用户导出的 4 个 Excel 即目标形状的样例：
- `销售价格查询.xlsx`(16列/1152行) ≈ 销售单据明细行：单据日期·单据编号·客户·部门·存货编码·存货·规格型号·计量单位·数量·折扣%·单价·金额·含税单价·含税金额·税额。
- `采购价格查询.xlsx`(21列/530行) ≈ 采购单据明细行：多了 供应商编码·供应商·供应商简称·业务员·仓库·项目。
- 两张"波动分析表"是带标题的透视报表（**本期不做**，仅在 catalog 标注"报表类，暂不同步"）。

**Files:**
- Modify: `AliECS/services/tplus-sync-worker/config/endpoints.py`
- Implement: `AliECS/services/tplus-sync-worker/src/tplus_datahub/modules/purchase_price/{sync,transform,export}_purchase_price.py`
- Implement: 同结构 `…/modules/sales_price/…`
- Test: `AliECS/tests/test_tplus_price.py`（新建）
- Fixtures: `AliECS/tests/fixtures/tplus_price/`（放脱敏后的 GetVoucherDTO JSON 样例 + 期望行）

- [ ] **Step 1: 摸清现有 voucher 模块范式**——读 `modules/voucher/sync_voucher_list.py`、`export_voucher_list.py`、`modules/base_archive/*`、`core/`（client/分页/落库/导出 Excel 的工具）、`jobs/_pending_job.py`、`jobs/job_sync_all.py`。**价格模块必须照搬 voucher 的 client 调用/分页/落库/导出风格**，不要另起炉灶。把范式记进 Task note。

- [ ] **Step 2: 用真实导出 Excel 反推期望列**——`openpyxl` 读 4 个文件（路径见上）的真实表头（终端里是 GBK 乱码，用 `openpyxl` 读 unicode 正常），在 `tests/fixtures/tplus_price/expected_columns.py` 固化两张查询表的中文列名顺序，作为 transform 输出契约。

- [ ] **Step 3: 写失败测试**——transform 把 GetVoucherDTO Details 拍平成价格行

```python
# tests/test_tplus_price.py
def test_transform_purchase_price_rows():
    dto = load_fixture("tests/fixtures/tplus_price/purchase_voucher_sample.json")
    rows = transform_purchase_price_rows([dto])
    r = rows[0]
    assert r["单据编号"].startswith("PS-")
    assert r["含税单价"] == pytest.approx(16.2)      # OrigTaxPrice
    assert r["单价"] == pytest.approx(14.34)          # OrigDiscountPrice(无税)
    assert set(EXPECTED_PURCHASE_COLUMNS).issubset(r.keys())
def test_transform_sales_price_rows():
    ...
```

> fixture `purchase_voucher_sample.json`：从已研究的 GetVoucherDTO 结构造一条**脱敏**样例（字段名按真实：`Details[].OrigTaxPrice/OrigDiscountPrice/Quantity/InventoryCode/InventoryName/...`，单头 `Code/VoucherDate/Partner/...`）。结构不确定的字段在 Step 6 实时验证时对齐。

- [ ] **Step 4: 跑红** → FAIL（`raise_pending`）。

- [ ] **Step 5: 实现 transform + sync + export**
  - `endpoints.py`：把 `purchase_price/sales_price` 从 `PENDING_ENDPOINTS` 移走，加价格用到的 voucher 端点（采购：`PurchaseArrivalOpenApi`/`PurchaseReceiveOpenApi`/`PurchaseOrderOpenApi` 之一含价；销售：`SaleDeliveryOpenApi`/`SaleOrderOpenApi`），实际选哪个以 Step 6 实拉对得上 Excel 为准（先按"到货/发货"含价单优先）。
  - `transform_<x>.py`：DTO.Details[] → 行 dict（列名 = EXPECTED 列）。无税价/含税价/税额/数量/金额按 Excel 字段映射；缺的列补 `None`。
  - `sync_<x>.py`：照 voucher 模块——`FindVoucherList` 翻页取 voucher id → 逐单 `GetVoucherDTO` → `transform` → 落库(若 voucher 模块落 PG 就落同构表 `tplus_purchase_price_rows`/`tplus_sales_price_rows`，建 `db/migrations` 迁移) + 导出 Excel 到 `OUTPUT_DIR`(`/app/output`)。
  - `export_<x>.py`：照 `export_voucher_list` 写 xlsx，文件名带日期，落到 catalog 能扫到的目录。

- [ ] **Step 6: 跑绿（离线）** — `PYTHONPATH=services/tplus-sync-worker/src pytest tests/test_tplus_price.py -v` → PASS。

- [ ] **Step 7: 实时验证（允许实拉，Q3）**——在 tplus-sync-worker 容器/本地用现有 `CHANJET_*` 凭据跑一次价格 job：

```bash
PYTHONPATH=services/tplus-sync-worker/src python -m tplus_datahub.jobs.job_sync_purchase_price
PYTHONPATH=services/tplus-sync-worker/src python -m tplus_datahub.jobs.job_sync_sales_price
```

  对比产出 xlsx 与用户 `采购价格查询.xlsx`/`销售价格查询.xlsx`：行数量级、关键列(含税单价/数量/存货编码)抽样一致。**外部失败处理**：401/token 问题→读记忆 `tplus-purchase-price-research`(openToken 已 webhook 自动换取)与 `CHANJET_OPEN_TOKEN_FILE`；连不上/超时→重试≤3 后记录"实时验证待人工"并继续 Phase 4，**不卡死**。把对比结果写进 `docs/ops/tplus-price-verify-2026-06-14.md`。

- [ ] **Step 8: 接入导出目录**——确认 `/v1/exports/catalog`(`main.py:1721`) 的 tplus tab 会扫到新 xlsx（多半自动，因为按目录扫）；若 catalog 有显式白名单，把两张价格表加进去。加一条 `tests/test_backend_exports.py` 风格断言：catalog 含 purchase_price/sales_price 条目。

- [ ] **Step 9: commit**

```bash
git add services/tplus-sync-worker config/endpoints.py tests/test_tplus_price.py tests/fixtures/tplus_price db/migrations docs/ops/tplus-price-verify-2026-06-14.md services/backend-api/app/main.py
git commit -m "feat(tplus): 实现采购/销售价格同步+导出, 接入数据导出目录"
```

**完成判据**：transform 单测绿；至少离线 fixtures 全绿；实拉若成功则 xlsx 与用户导出抽样一致、`/health/数据导出` 出现价格 tab；实拉失败也留下完整代码+待验证记录。

---

## Phase 4 — ② 企微A 控制面板 → DB → 运行时（~50 min，设计重，混合真相源）

**决策（Q2）**：企微A 智能表「管理面板」人工可编辑 → doc-sync **拉表 upsert 进 Postgres** → Postgres 为运行时真相源（backend 生成路由/权限，webdock 拉取消费）。

**统一用户模型（计划默认，未单独追问）**：建**一张** `managed_contacts` 表用 `channel` 区分微信/飞书；企微表里"微信用户清单/飞书用户清单"是**按 channel 过滤的两个 worksheet（视图）**，写回同一张表。

**Files:**
- Create: `AliECS/db/migrations/<n>_managed_contacts.sql`
- Modify: `AliECS/services/doc-sync-worker/app/…`（新增 worksheet→`managed_contacts` 的 sync 源；照搬现有企微智能表读取范式）
- Modify: `AliECS/services/backend-api/app/main.py`（暴露 `/v1/routing/wechat-projects.json`、`/v1/routing/feishu-projects.json`、`/v1/admin/contacts` 读写）
- Modify: `webdock/src/browser/lane_routing.py` + 一个拉取器（webdock 定时 GET backend 路由 → 写本地 `wechat_projects.json`，保留 graceful-degrade）
- Test: `AliECS/tests/test_managed_contacts_sync.py`、`AliECS/tests/test_routing_api.py`、`webdock/tests/test_routing_pull.py`

- [ ] **Step 1: 设计表**——`managed_contacts` 字段（尽量多承载用户信息，用户item②"尽可能多字段"）：

```sql
CREATE TABLE managed_contacts (
  id            BIGSERIAL PRIMARY KEY,
  channel       TEXT NOT NULL,                 -- 'wechat' | 'feishu'
  peer_id       TEXT NOT NULL,                 -- 渠道内唯一标识(微信peer/飞书open_id)
  display_name  TEXT,
  remark        TEXT,                          -- 备注/真名
  enabled       BOOLEAN NOT NULL DEFAULT true, -- 权限总开关(人工改表即生效)
  project_url   TEXT,                          -- ChatGPT 项目地址(换它=换项目重开对话)
  project_name  TEXT,
  tags          TEXT,                          -- 逗号分隔标签/分组
  daily_quota   INTEGER,                       -- 预留:配额
  notes         TEXT,
  source_sheet  TEXT,                          -- 来源worksheet名
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(channel, peer_id)
);
```

- [ ] **Step 2: 写失败测试**——doc-sync 把企微 worksheet 行 upsert 成 contacts

```python
# tests/test_managed_contacts_sync.py（仿 test_doc_sync_worker.py 的桩）
def test_sync_worksheet_upserts_contacts(fake_wecom_smartsheet, db):
    fake_wecom_smartsheet.set_rows("微信用户清单", [
        {"peer_id": "wxid_a", "display_name": "张三", "enabled": "是", "project_url": "https://chatgpt.com/g/p1"},
    ])
    sync_managed_contacts(profiles=["wecom_company_a"])
    row = db.one("SELECT channel,enabled,project_url FROM managed_contacts WHERE peer_id='wxid_a'")
    assert row["channel"] == "wechat" and row["enabled"] is True
    # 改表:关权限+换项目 → 再同步即生效
    fake_wecom_smartsheet.set_rows("微信用户清单", [{"peer_id":"wxid_a","enabled":"否","project_url":"https://chatgpt.com/g/p2"}])
    sync_managed_contacts(profiles=["wecom_company_a"])
    row = db.one("SELECT enabled,project_url FROM managed_contacts WHERE peer_id='wxid_a'")
    assert row["enabled"] is False and row["project_url"].endswith("p2")
```

- [ ] **Step 3: 跑红 → 实现 doc-sync 源**——读现有 doc-sync 怎么读企微智能表(`WECOM_*`/`SMARTSHEET_COMPANY_A_*`，见 compose:100-102)，加一个把指定 worksheet（"微信用户清单"→channel=wechat、"飞书用户清单"→channel=feishu）逐行 upsert 进 `managed_contacts` 的 source。worksheet 名→channel 映射做成配置。enabled 列容忍"是/否/true/false/1/0"。

- [ ] **Step 4: 跑绿** — `pytest tests/test_managed_contacts_sync.py -v` → PASS。

- [ ] **Step 5: 后端路由 API（DB→运行时）**——写 `/v1/routing/wechat-projects.json` 生成 webdock 期望的结构（见 `lane_routing.py:51`）：

```json
{"lanes": {"<peer_id>": {"name": "...", "project_url": ".../project"}}}
```

只含 `channel='wechat' AND enabled AND project_url IS NOT NULL`。再写 feishu 版。加 `tests/test_routing_api.py` 断言只输出 enabled 行、结构匹配 webdock。

- [ ] **Step 6: webdock 拉取消费**——`webdock` 加定时拉取器：周期 GET backend `/v1/routing/wechat-projects.json`（经 webdock→ECS 的现有通路或直连 backend 内网），写入 `LaneRouter` 读的本地 `wechat_projects.json`；backend 不可达时**保留旧文件**（graceful-degrade，见 `lane_routing.py:57`）。`webdock/tests/test_routing_pull.py` 断言：拉取成功覆盖、失败保旧。

- [ ] **Step 7: 跑绿全 Phase** — 两仓相关测试全绿。

- [ ] **Step 8: commit（分两仓）**

```bash
cd AliECS && git add db/migrations services/doc-sync-worker services/backend-api/app/main.py tests/test_managed_contacts_sync.py tests/test_routing_api.py && git commit -m "feat(control-plane): 企微A表→managed_contacts→运行时路由/权限API"
cd ../webdock && git add src tests/test_routing_pull.py && git commit -m "feat(routing): 从backend拉取路由覆盖本地json(失败保旧)"
```

- [ ] **⚠️ ops 步骤（交用户）**：在企微A智能表「管理面板」建/规范 `微信用户清单`、`飞书用户清单` worksheet 及列（peer_id/display_name/enabled/project_url/...，列名与 Step3 映射对齐）；配置 doc-sync 的 `SMARTSHEET_COMPANY_A_*` 指向该表。Codex 在 `docs/ops/control-plane-sheet-schema.md` 输出列规范交付。

**完成判据**：改企微表字段→doc-sync 同步→`managed_contacts` 变化→路由 API 输出变化→webdock 拉到新路由（链路各段有测试覆盖；端到端需 ops 配好表后人工点一次同步验证）。

---

## Phase 5 — ① 飞书一对一打通（~30 min，依赖 Phase4 用户模型 + 实时飞书）

**现状**：OpenClaw `channels.feishu` **已 enabled**（websocket / dmPolicy=open / allowFrom=["*"] / groupPolicy=open / requireMention=false，实测于本机 `openclaw.json`）。缺的是：① 端到端确认（飞书开放平台事件订阅 + `FEISHU_APP_SECRET` env + websocket 实连）；② bridge/webdock 对**飞书 peer** 的 lane 隔离与路由（现路由按 peer_id，飞书 open_id 是另一种 id 空间）。

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`（确认元数据清洗/ lane 继承不丢飞书 channel/peer）
- Modify: `webdock/src/browser/lane_routing.py`（飞书 peer 也走 Phase4 的 feishu 路由）
- Test: `AliECS/tests/test_openclaw_bridge.py`（加飞书 peer 用例）、`webdock/tests/`（飞书 lane 隔离）

- [ ] **Step 1: 写失败测试**——bridge 对飞书来源消息保留独立 lane

```python
# tests/test_openclaw_bridge.py 追加
def test_feishu_peer_gets_isolated_lane():
    # 构造一条带 feishu channel + open_id 的请求, 断言 bridge 解析出的 lane key 与微信 peer 不串
    ...
```

- [ ] **Step 2: 跑红 → 实现**——核对 bridge 从 OpenClaw 收到的 metadata 里 channel/peer 字段，确保飞书消息生成 `feishu:<open_id>` 形态 lane，并能映射到 Phase4 的 feishu 路由（每个飞书用户独立 ChatGPT 会话/项目）。webdock `LaneRouter` 支持 feishu key。

- [ ] **Step 3: 跑绿** — `pytest tests/test_openclaw_bridge.py -v` + webdock 路由测试 → PASS。

- [ ] **Step 4: 实时端到端（允许，Q3）**——确认主机 `FEISHU_APP_SECRET` 在 OpenClaw `.env`（`appSecret.id=FEISHU_APP_SECRET`）；查 OpenClaw 日志 feishu websocket 是否 connected：

```bash
ssh aliecs "docker logs --since 5m openclaw-openclaw-gateway-1 2>&1 | grep -iE 'feishu|lark|websocket' | tail"
```

  用飞书给机器人发一条 DM，跟 `chain.jsonl`/webdock archive 看是否走通 ChatGPT 并回流。**外部失败处理**：websocket 未连/无回 → 记录到 `docs/ops/feishu-channel-verify-2026-06-14.md` 并列出"需在飞书开放平台后台开的事件订阅/权限"清单，继续 Phase 6，不卡死。

- [ ] **Step 5: commit**

```bash
cd AliECS && git add deploy/openclaw-bridge/openclaw_bridge.py tests/test_openclaw_bridge.py docs/ops/feishu-channel-verify-2026-06-14.md && git commit -m "feat(feishu): bridge/webdock 飞书lane隔离与路由打通"
cd ../webdock && git add src tests && git commit -m "feat(feishu): webdock 支持飞书lane路由"
```

- [ ] **⚠️ ops 步骤（交用户）**：飞书开放平台后台确认「事件订阅(接收消息)」「机器人能力」「联系人/消息权限」已开、应用已发布版本；`FEISHU_APP_SECRET` 已注入 OpenClaw `.env`。

**完成判据**：飞书 lane 隔离有单测；实连成功则任意飞书用户 DM 能一对一聊 ChatGPT；未连成留清单。

---

## Phase 6 — ③ /health/ 加微信入口 + 加微信二维码（~25 min，依赖 weixin 登录态）

**现状**：加微信二维码 = OpenClaw weixin 渠道登录态产物（`/root/openclaw/docs/channels/wechat.md`、`pairing.md`），非 webdock 现有功能。需把"当前 weixin 登录/配对二维码"从网关侧取出，放进 `/health/` 一个功能区入口。

**Files:**
- Modify: `AliECS/services/public-web/health/index.html`（加"功能区"入口 + "添加新微信"按钮 + 二维码弹窗）
- Modify: `AliECS/services/backend-api/app/main.py`（新增 `/v1/ops/wechat/login-qr` 代理取二维码）
- Test: `AliECS/tests/test_wechat_login_qr.py`

- [ ] **Step 1: 调研 weixin 取码方式**——读 `ssh aliecs` 上 `/root/openclaw/docs/channels/wechat.md` 与 `pairing.md`，确认 OpenClaw 暴露登录/配对二维码的方式（网关 API？CLI？日志里的二维码图/串？）。把结论写进 Task note。**若 OpenClaw 无稳定取码 API**：降级为"管理员触发→网关侧生成二维码图片落到 backend 可读位置→前端展示"，并把限制写清。

- [ ] **Step 2: 写失败测试**——后端取码端点（mock 网关返回）

```python
# tests/test_wechat_login_qr.py
def test_login_qr_endpoint_returns_image_or_url(admin_client, monkeypatch):
    # mock 调 OpenClaw 网关取码, 断言返回 {qr_image_base64|qr_url, expires_at}
    r = admin_client.get("/v1/ops/wechat/login-qr")
    assert r.status_code == 200 and ("qr_image_base64" in r.json() or "qr_url" in r.json())
```

- [ ] **Step 3: 跑红 → 实现**——`/v1/ops/wechat/login-qr`（require_admin）调网关取码并回前端可渲染结构。

- [ ] **Step 4: 前端**——`/health/` 顶部加"功能区"，含"添加新微信"按钮；点击 → 弹窗调 `/v1/ops/wechat/login-qr` 渲染二维码 + "等待新用户扫码加入 微信clawbot"提示 + 刷新按钮（码会过期）。沿用 health 页现有 modal/按钮风格。

- [ ] **Step 5: 跑绿** — `pytest tests/test_wechat_login_qr.py -v` → PASS。

- [ ] **Step 6: 实时验证（允许，Q3）**——admin 打开 /health/ 点"添加新微信"，确认弹出真实二维码、手机可扫。失败→记 `docs/ops/wechat-add-verify-2026-06-14.md` 并继续。

- [ ] **Step 7: commit**

```bash
git add services/public-web/health/index.html services/backend-api/app/main.py tests/test_wechat_login_qr.py docs/ops/wechat-add-verify-2026-06-14.md
git commit -m "feat(ops): /health/ 功能区+添加新微信二维码入口"
```

**完成判据**：后端取码端点有测试；/health/ 有"添加新微信"入口能弹码；扫码加入需真机验证。

---

## Phase 7 — ⑦ 企微B 群历史 可行性 spike + 前向捕获（~20 min，研究为主）

**背景**：企微B 已加机器人「项目开发管理助手」（长连接 SDK：Bot ID `aibQWXWe4Vz_eJ7-zk3ewCBe-L6c4VOUNMK` + Secret；权限：用户信息默认授权、文档已授权）。用户问：**能否拿到群里所有历史聊天记录**。

**几乎确定的结论**：企微智能机器人/长连接 SDK 只能接收**加入后**推送的新消息（回调/长连接），**无"拉取历史群聊"的 API**；历史消息归档属于「会话内容存档」另一套企业级能力（需单独开通+加密证书+合规授权），与机器人 Bot 不是一回事。

**Files:**
- Create: `AliECS/docs/ops/wecom-b-group-history-spike-2026-06-14.md`（结论报告）
- Optional: `AliECS/services/backend-api/app/main.py` + `tests/`（前向捕获 webhook 接收器，若决定做）

- [ ] **Step 1: 核实（允许实查，Q3）**——用 Bot 凭据查企微 OpenAPI 文档/能力：是否存在群历史拉取接口；「会话内容存档」是否对该应用开通。把证据（接口名/报错/文档链接）写进 spike 报告，给出明确"能/不能 + 为什么 + 要拿历史需走什么合规路径"。

- [ ] **Step 2: 前向捕获接收器（可做的那部分）**——若用户要"从现在起"留存：实现一个接收企微长连接/回调新消息并落 `wecom_b_messages` 表的最小端点 + 单测（mock 一条消息→落库）。这是**唯一无需合规存档即可拿到**的群消息（仅机器人可见范围、加入后）。

- [ ] **Step 3: commit**

```bash
git add docs/ops/wecom-b-group-history-spike-2026-06-14.md services/backend-api/app/main.py tests/test_wecom_b_capture.py
git commit -m "docs(wecom-b): 群历史可行性结论 + 前向消息捕获接收器"
```

**完成判据**：一份有依据的可行性报告（结论=历史拿不到，给替代路径）；可选的前向捕获接收器有单测。

---

## 收尾（Codex 跑完后）

- [ ] **汇总**：在 `docs/ops/codex-run-2026-06-14-summary.md` 写：每个 Phase 完成度（全done / 仅离线 / 待ops）、新增测试数、未决的"⚠️ ops 步骤"清单、外部实拉成败。
- [ ] **不要 push / 不要合并**——停在 `codex/project-completion-2026-06-14` 分支等用户 review。
- [ ] 跑全量：`cd AliECS && pytest tests/ -q` 与 `cd webdock && pytest -q`，把最终绿/跳过数写进 summary。

## 自检（写计划时已核对）
- **Spec 覆盖**：①Phase5 ②Phase4 ③Phase6 ④Phase1 ⑤Phase3 ⑥Phase2 ⑦Phase7 —— 7 项各有归属。
- **排序**：按"无人值守可独立完成度"= Phase1(纯本地)→2(纯本地)→3(fixtures+实拉)→4(设计重/本地可测)→5/6(依赖实时外部)→7(研究)。符合 Q1。
- **决策落地**：Q2 混合源(Phase4 表→DB→运行时)、Q3 允许实拉+单分支逐任务提交(全局规则)、Q4 couple 双保险(Phase2)。
- **外部依赖**：每个碰外部的 Phase 都有"fixtures 先全绿 + 实时验证单列 + 失败不卡死 + ops 清单"，保证 3h 不停跑。
