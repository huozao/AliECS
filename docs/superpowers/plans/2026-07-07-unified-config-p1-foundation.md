# 统一用户配置 · 计划① 地基（bridge 建列助手 + app_token 参数化） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给飞书 Bitable 建列助手加上"建单选/多选字段并带选项"与"指定 app_token"两项能力，作为独立「系统配置」多维表格与"选项化"配置的地基。

**Architecture:** 全部改动集中在单文件 `deploy/openclaw-bridge/openclaw_bridge.py` 的 Bitable 字段助手区（1215–1293 行附近）。新增能力全部以**可选参数**加入，默认值保持现有调用行为不变（向后兼容）。纯逻辑（选项属性构造）抽成无网络的函数便于单测；建列/列举/删列的网络函数用 monkeypatch 捕获请求路径与 body 验证。

**Tech Stack:** Python 3.12，pytest；bridge 单测用 `importlib` 从文件路径加载模块（见 `tests/test_openclaw_bridge.py:load_bridge`）。

## Global Constraints

- 所有新增能力用**可选参数**（默认 `None`/沿用 `feishu_session_console_app_token()`），**不得改变现有调用方行为**。
- best-effort 契约：建列/删列/列举失败只 `log_line` 跳过，**绝不抛出**（沿用 `ensure_feishu_bitable_fields` 现有约定）。
- 复用已有先例：单选 payload 形态见 `deploy/openclaw-bridge/migrate_feishu_bitable_links.py:single_select_field_payload`（`type=3` + `property.options=[{"name","color"}]`）。多选同构，`type=4`。
- Feishu 字段类型码：文本=1、数字=2、单选=3、多选=4、复选框=7。
- 测试从仓库根跑：`pytest tests/test_openclaw_bridge.py::<name> -v`。
- 本计划只交付**代码+测试**；实际新建飞书簿、写 SOPS、bridge cutover 属部署动作，见末尾"部署（计划外，人工）"。

---

### Task 1: 单选/多选字段属性构造 + 类型常量

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`（在 `FEISHU_BITABLE_FIELD_TYPE_CHECKBOX = 7` 附近，1215–1216 行）
- Test: `tests/test_openclaw_bridge.py`

**Interfaces:**
- Produces:
  - 常量 `FEISHU_BITABLE_FIELD_TYPE_SINGLE_SELECT = 3`、`FEISHU_BITABLE_FIELD_TYPE_MULTI_SELECT = 4`
  - `feishu_select_field_property(options: list[str]) -> dict[str, Any]` → `{"options": [{"name": str, "color": int}, ...]}`，保序、`color = index % 16`。

- [ ] **Step 1: Write the failing test**

在 `tests/test_openclaw_bridge.py` 末尾追加：

```python
def test_select_field_property_keeps_order_and_colors():
    bridge = load_bridge()
    prop = bridge.feishu_select_field_property(["极速", "均衡", "高级"])
    assert [o["name"] for o in prop["options"]] == ["极速", "均衡", "高级"]
    assert [o["color"] for o in prop["options"]] == [0, 1, 2]


def test_select_field_type_constants():
    bridge = load_bridge()
    assert bridge.FEISHU_BITABLE_FIELD_TYPE_SINGLE_SELECT == 3
    assert bridge.FEISHU_BITABLE_FIELD_TYPE_MULTI_SELECT == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_openclaw_bridge.py::test_select_field_property_keeps_order_and_colors tests/test_openclaw_bridge.py::test_select_field_type_constants -v`
Expected: FAIL（`AttributeError: module 'openclaw_bridge' has no attribute 'feishu_select_field_property'` / 常量缺失）

- [ ] **Step 3: Write minimal implementation**

在 `openclaw_bridge.py` 的 `FEISHU_BITABLE_FIELD_TYPE_CHECKBOX = 7` 之后加：

```python
FEISHU_BITABLE_FIELD_TYPE_SINGLE_SELECT = 3
FEISHU_BITABLE_FIELD_TYPE_MULTI_SELECT = 4


def feishu_select_field_property(options: list[str]) -> dict[str, Any]:
    """单选/多选字段的 property：保序选项，color 轮转 0..15。"""
    return {"options": [{"name": name, "color": index % 16} for index, name in enumerate(options)]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_openclaw_bridge.py::test_select_field_property_keeps_order_and_colors tests/test_openclaw_bridge.py::test_select_field_type_constants -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add deploy/openclaw-bridge/openclaw_bridge.py tests/test_openclaw_bridge.py
git commit -m "feat(bridge): 单选/多选字段属性构造 + 类型常量"
```

---

### Task 2: app_token 参数化四个 Bitable 字段助手

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`
  - `list_feishu_bitable_fields`（1219–1226）
  - `create_feishu_bitable_field`（1229–1238）
  - `delete_feishu_bitable_field`（1241–1248）
  - `ensure_feishu_bitable_fields`（1251–1293）
- Test: `tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `feishu_session_console_app_token()`（现有）
- Produces（四个函数新增可选首选参数 `app_token: str | None = None`；`None` 时回退 `feishu_session_console_app_token()`）：
  - `list_feishu_bitable_fields(table_id, app_token=None) -> list[dict]`
  - `create_feishu_bitable_field(table_id, field_name, field_type=7, app_token=None) -> dict`
  - `delete_feishu_bitable_field(table_id, field_id, app_token=None) -> dict`
  - `ensure_feishu_bitable_fields(table_id, field_names, field_type=7, *, reconcile_type=False, app_token=None) -> None`

- [ ] **Step 1: Write the failing test**

```python
def test_create_field_routes_explicit_app_token(monkeypatch):
    bridge = load_bridge()
    seen = {}

    def fake_post(path, body):
        seen["path"] = path
        seen["body"] = body
        return {}

    monkeypatch.setattr(bridge, "feishu_post_json", fake_post)
    # 显式 app_token 不应回退到会话台 app_token
    monkeypatch.setattr(bridge, "feishu_session_console_app_token", lambda: "SESSION_TOKEN")
    bridge.create_feishu_bitable_field("tblX", "开关", app_token="SYSCFG_TOKEN")
    assert "SYSCFG_TOKEN" in seen["path"]
    assert "SESSION_TOKEN" not in seen["path"]


def test_create_field_defaults_to_session_app_token(monkeypatch):
    bridge = load_bridge()
    seen = {}
    monkeypatch.setattr(bridge, "feishu_post_json", lambda path, body: seen.setdefault("path", path) or {})
    monkeypatch.setattr(bridge, "feishu_session_console_app_token", lambda: "SESSION_TOKEN")
    bridge.create_feishu_bitable_field("tblX", "开关")
    assert "SESSION_TOKEN" in seen["path"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_openclaw_bridge.py::test_create_field_routes_explicit_app_token tests/test_openclaw_bridge.py::test_create_field_defaults_to_session_app_token -v`
Expected: FAIL（`create_feishu_bitable_field() got an unexpected keyword argument 'app_token'`）

- [ ] **Step 3: Write minimal implementation**

四个函数各加一行 `app_token = app_token or feishu_session_console_app_token()`，签名加 `app_token: str | None = None`。示例（`create_feishu_bitable_field`）：

```python
def create_feishu_bitable_field(
    table_id: str,
    field_name: str,
    field_type: int = FEISHU_BITABLE_FIELD_TYPE_CHECKBOX,
    app_token: str | None = None,
) -> dict[str, Any]:
    app_token = app_token or feishu_session_console_app_token()
    if not app_token or not table_id:
        return {}
    return feishu_post_json(
        f"/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables/{urllib.parse.quote(table_id)}/fields",
        {"field_name": field_name, "type": field_type},
    )
```

`list_feishu_bitable_fields` / `delete_feishu_bitable_field` 同样把开头的 `app_token = feishu_session_console_app_token()` 改成 `app_token = app_token or feishu_session_console_app_token()` 并加参数。`ensure_feishu_bitable_fields` 加 `app_token=None` 关键字参数，并在内部把 `list_feishu_bitable_fields(table_id)` / `create_feishu_bitable_field(table_id, name, field_type)` / `delete_feishu_bitable_field(table_id, ...)` 三处调用都透传 `app_token=app_token`。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_openclaw_bridge.py::test_create_field_routes_explicit_app_token tests/test_openclaw_bridge.py::test_create_field_defaults_to_session_app_token -v`
Expected: PASS

- [ ] **Step 5: 回归既有建列测试**

Run: `pytest tests/test_openclaw_bridge.py -k "field or rule_record" -v`
Expected: PASS（`test_ensure_rule_record_creates_with_new_fields` 等既有测试不受影响）

- [ ] **Step 6: Commit**

```bash
git add deploy/openclaw-bridge/openclaw_bridge.py tests/test_openclaw_bridge.py
git commit -m "feat(bridge): Bitable 字段助手支持指定 app_token（默认回退会话台）"
```

---

### Task 3: create/ensure 支持 field_property（建单选/多选带选项）

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`（`create_feishu_bitable_field`、`ensure_feishu_bitable_fields`）
- Test: `tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: Task 1 的 `feishu_select_field_property`、类型常量；Task 2 的 `app_token` 形参。
- Produces（再加可选 `field_property: dict | None = None`）：
  - `create_feishu_bitable_field(table_id, field_name, field_type=7, app_token=None, field_property=None) -> dict`：`field_property` 非空时并入 POST body 的 `property`。
  - `ensure_feishu_bitable_fields(table_id, field_names, field_type=7, *, reconcile_type=False, app_token=None, field_property=None) -> None`：透传给 `create_feishu_bitable_field`。

- [ ] **Step 1: Write the failing test**

```python
def test_create_single_select_field_posts_options(monkeypatch):
    bridge = load_bridge()
    seen = {}
    monkeypatch.setattr(bridge, "feishu_post_json", lambda path, body: seen.setdefault("body", body) or {})
    monkeypatch.setattr(bridge, "feishu_session_console_app_token", lambda: "T")
    bridge.create_feishu_bitable_field(
        "tblX",
        "对话模式默认",
        field_type=bridge.FEISHU_BITABLE_FIELD_TYPE_SINGLE_SELECT,
        field_property=bridge.feishu_select_field_property(["极速", "均衡", "高级"]),
    )
    assert seen["body"]["type"] == 3
    assert [o["name"] for o in seen["body"]["property"]["options"]] == ["极速", "均衡", "高级"]


def test_create_field_without_property_omits_property_key(monkeypatch):
    bridge = load_bridge()
    seen = {}
    monkeypatch.setattr(bridge, "feishu_post_json", lambda path, body: seen.setdefault("body", body) or {})
    monkeypatch.setattr(bridge, "feishu_session_console_app_token", lambda: "T")
    bridge.create_feishu_bitable_field("tblX", "开关")
    assert "property" not in seen["body"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_openclaw_bridge.py::test_create_single_select_field_posts_options tests/test_openclaw_bridge.py::test_create_field_without_property_omits_property_key -v`
Expected: FAIL（`unexpected keyword argument 'field_property'`）

- [ ] **Step 3: Write minimal implementation**

`create_feishu_bitable_field` 改为：

```python
def create_feishu_bitable_field(
    table_id: str,
    field_name: str,
    field_type: int = FEISHU_BITABLE_FIELD_TYPE_CHECKBOX,
    app_token: str | None = None,
    field_property: dict[str, Any] | None = None,
) -> dict[str, Any]:
    app_token = app_token or feishu_session_console_app_token()
    if not app_token or not table_id:
        return {}
    body: dict[str, Any] = {"field_name": field_name, "type": field_type}
    if field_property:
        body["property"] = field_property
    return feishu_post_json(
        f"/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables/{urllib.parse.quote(table_id)}/fields",
        body,
    )
```

`ensure_feishu_bitable_fields` 签名加 `field_property: dict[str, Any] | None = None`，把内部建列调用改成 `create_feishu_bitable_field(table_id, name, field_type, app_token=app_token, field_property=field_property)`。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_openclaw_bridge.py::test_create_single_select_field_posts_options tests/test_openclaw_bridge.py::test_create_field_without_property_omits_property_key -v`
Expected: PASS

- [ ] **Step 5: 全量回归 bridge 测试**

Run: `pytest tests/test_openclaw_bridge.py -q`
Expected: PASS（全绿，无回归）

- [ ] **Step 6: Commit**

```bash
git add deploy/openclaw-bridge/openclaw_bridge.py tests/test_openclaw_bridge.py
git commit -m "feat(bridge): 建列助手支持 field_property，可建带选项的单选/多选"
```

---

### Task 4: 「系统配置」簿的 app_token/table 访问器（env）

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`（在 `feishu_session_console_app_token` 附近，810 行区）
- Test: `tests/test_openclaw_bridge.py`

**Interfaces:**
- Produces:
  - `system_config_app_token() -> str`：读 `FEISHU_SYSTEM_CONFIG_APP_TOKEN`，缺失返回 `""`（**不回退**会话台，独立簿是独立 token）。
  - `system_config_table_id(name: str) -> str`：读 `FEISHU_SYSTEM_CONFIG_<NAME>_TABLE_ID`（`name` 大写），缺失返回 `""`。

说明：这是给后续计划（对话模式默认、生效面）指向独立簿用的 env 访问器。真实 token/表 id 由部署步骤写入 SOPS，本任务只交付代码 + 测试。

- [ ] **Step 1: Write the failing test**

```python
def test_system_config_app_token_reads_env(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SYSTEM_CONFIG_APP_TOKEN", "SYSCFG")
    assert bridge.system_config_app_token() == "SYSCFG"


def test_system_config_app_token_no_session_fallback(monkeypatch):
    bridge = load_bridge()
    monkeypatch.delenv("FEISHU_SYSTEM_CONFIG_APP_TOKEN", raising=False)
    monkeypatch.setattr(bridge, "feishu_session_console_app_token", lambda: "SESSION")
    assert bridge.system_config_app_token() == ""


def test_system_config_table_id_reads_named_env(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SYSTEM_CONFIG_CHAT_MODE_TABLE_ID", "tblMode")
    assert bridge.system_config_table_id("chat_mode") == "tblMode"
    monkeypatch.delenv("FEISHU_SYSTEM_CONFIG_CHAT_MODE_TABLE_ID", raising=False)
    assert bridge.system_config_table_id("chat_mode") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_openclaw_bridge.py -k system_config -v`
Expected: FAIL（`has no attribute 'system_config_app_token'`）

- [ ] **Step 3: Write minimal implementation**

```python
def system_config_app_token() -> str:
    """独立「系统配置」多维表格的 app_token；缺失返回空（不回退会话台）。"""
    return os.getenv("FEISHU_SYSTEM_CONFIG_APP_TOKEN") or ""


def system_config_table_id(name: str) -> str:
    """按域名取「系统配置」簿的 table_id：FEISHU_SYSTEM_CONFIG_<NAME>_TABLE_ID。"""
    return os.getenv(f"FEISHU_SYSTEM_CONFIG_{name.upper()}_TABLE_ID") or ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_openclaw_bridge.py -k system_config -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add deploy/openclaw-bridge/openclaw_bridge.py tests/test_openclaw_bridge.py
git commit -m "feat(bridge): 新增「系统配置」簿 app_token/table_id env 访问器"
```

---

## 部署（计划外，人工，落地时执行）

1. 在飞书新建一个多维表格 App 作为「系统配置」簿，把 bridge 应用加为协作者。
2. 记录其 `app_token` 与各域表 `table_id`，用 `sops set` 写入 `infra/secrets/server.enc.env`：`FEISHU_SYSTEM_CONFIG_APP_TOKEN`、`FEISHU_SYSTEM_CONFIG_<NAME>_TABLE_ID`。
3. push 到 origin + 各设备 bare（见 [[multi-party-code-consistency]]），ECS `render.sh` 后 force-recreate bridge（env_file 只在创建时读）。
4. 合并 PR → `bridge-cutover.yml`（人工触发）切镜像，`/v1/models` 验证健康。

> 本计划的四个任务只交付代码 + 单测；上面的 1–4 是把地基投产的运维动作，放到"迁配置"计划真正需要独立簿时一起做。

---

## Self-Review

**Spec 覆盖（对 spec 的 D4 地基三条）：**
- D4-地基①「建列助手无 property，需扩展」→ Task 1 + Task 3 ✅
- D4-地基②「app_token 写死，需参数化」→ Task 2 ✅
- D4-地基③「单选/多选选项幂等对齐」→ **本计划只覆盖"建列时写入选项"（静态枚举）**；已存在字段的选项对齐/动态同步（仓库）明确留给计划③（backend 迁仓库范围）——非本计划缺口，是分期边界。
- 独立簿访问器（D1）→ Task 4 ✅

**Placeholder 扫描：** 无 TBD/TODO；每个改码步骤都给了完整代码。

**类型一致性：** `feishu_select_field_property(options: list[str]) -> dict`（Task 1 定义）在 Task 3 测试中按 `{"property":{"options":[{"name",...}]}}` 消费，一致；`app_token`/`field_property` 形参名在 Task 2/3 create 与 ensure 间一致。

**范围：** 单文件、四个小任务，各自独立可测，符合"独立可测软件"。后续计划（②③④⑤）在地基之上另立。
