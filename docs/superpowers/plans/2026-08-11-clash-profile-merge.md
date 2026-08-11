# Clash 配置合成器实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 admin-ui 增加一个页面，把自建节点与第三方机场订阅合成为一份 Clash 配置文件供下载，客户端只需一份配置即可自由选择两边节点。

**Architecture:** 服务端不接触机场——机场节点交给 mihomo 的 `proxy-providers` 由客户端自行拉取与定期刷新。服务端只做「静态模板 + 运行时节点定义 → 单一 YAML 文本」的拼接。静态段原样输出，动态段用 `json.dumps` 生成（YAML 1.2 是 JSON 超集），因此全程不需要 YAML 解析器。

**Tech Stack:** FastAPI / psycopg3 / PostgreSQL / 原生 HTML+JS（admin-ui 单文件）/ Python 标准库 `json`、`ipaddress`

设计文档：`docs/superpowers/specs/2026-08-11-clash-profile-merge-design.md`

## Global Constraints

- **零新增依赖。** 不得往 `services/backend-api/requirements.txt` 加任何包。不得 `import yaml`。
- **AliECS 是 PUBLIC 仓库。** 以下内容一律不得出现在任何被提交的文件（含代码注释、文档、测试 fixture）中：自建节点的服务器地址、端口、传输与伪装参数、任何凭据、节点名；第三方机场的名称与订阅 URL。测试里一律用 `example.com` / `203.0.113.10`（RFC 5737 文档专用地址）这类占位值。
- **代理组名 `节点选择` 不可更改。** `template_base.yaml` 的 DNS 段有约 24 处 `#节点选择` 形式的引用，`#` 后是代理组名。改名会让境外 DNS 解析全部失效，且症状间歇、难排查。Task 1 有专门的回归断言保护它。
- **分支与提交**：AliECS 走分支 + PR，不直推 main。所有写 `.git` 的命令串行执行。提交前确认 `.env`、logs、`browser_data`、`_references`、真实密钥不在暂存区。
- **测试命令**：`python -m unittest discover -s tests`（在 `AliECS/` 目录下执行）。只写 `unittest.TestCase` 子类——`unittest discover` 不会执行 pytest 风格的裸函数。
- **PR 必须记录** `Nav-Impact: updated`。

## File Structure

| 文件 | 责任 |
|---|---|
| `services/backend-api/app/clash_profile/__init__.py` | 空包标记 |
| `services/backend-api/app/clash_profile/template_base.yaml` | 静态段：基础设置、`profile`、`sniffer`、`dns`、`tun`。原样输出，不含任何节点信息 |
| `services/backend-api/app/clash_profile/template_rules.yaml` | 静态段：规则列表项。**不含 `rules:` 这个 key**，只有 `  - XXX` 行 |
| `services/backend-api/app/clash_profile/render.py` | 纯函数，无 IO（除读自己目录下的两个模板），无数据库，无网络 |
| `services/backend-api/app/routers/clash_profile.py` | HTTP 层：机场源 CRUD + 生成下载。读环境变量、读数据库 |
| `services/backend-api/app/main.py` | 挂载路由 |
| `db/migrations/0048_clash_profile.sql` | 新表 `clash_profile_providers` |
| `services/admin-ui/index.html` | 新增页签与交互 |
| `tests/test_clash_profile_render.py` | 渲染纯函数单测 |
| `tests/test_clash_profile_router.py` | 环境变量解析单测 |
| `tests/test_admin_frontend.py` | 追加前端断言 |

模板拆成两个文件而不是一个，是为了让 `render.py` 能在 `rules:` 开头插入运行时推导的防回环规则，而不需要解析 YAML。

---

### Task 1: 渲染核心（模板 + 纯函数）

**Files:**
- Create: `services/backend-api/app/clash_profile/__init__.py`
- Create: `services/backend-api/app/clash_profile/template_base.yaml`
- Create: `services/backend-api/app/clash_profile/template_rules.yaml`
- Create: `services/backend-api/app/clash_profile/render.py`
- Test: `tests/test_clash_profile_render.py`

**Interfaces:**
- Consumes: 无（本任务是最底层）
- Produces:
  - `render_profile(self_nodes: list[dict], providers: list[dict]) -> str`
  - `provider_key(provider_id: int) -> str`，返回 `f"airport{provider_id}"`
  - 常量 `GROUP_SELECT = "节点选择"`、`GROUP_AUTO = "自动选择"`、`GROUP_AI = "AI服务"`
  - `self_nodes` 每个元素是一个完整的 clash proxy 定义 dict，必须含 `name` 与 `server` 两个键
  - `providers` 每个元素是 dict，键为 `id: int`、`name: str`、`url: str`、`enabled: bool`、`sort_order: int`
  - 自建节点为空时抛 `ValueError`

- [ ] **Step 1: 建分支**

```bash
git switch -c feat/clash-profile-merge
```

- [ ] **Step 2: 从 devbox 现用配置抽出两个静态模板**

devbox 上 Clash Verge 现用的本地 profile 结构顺序固定：基础设置 → `profile` → `sniffer` → `dns` → `tun` → `proxies` → `proxy-groups` → `rules`。按 `proxies:` 和 `rules:` 两行切开即可，无需解析 YAML。

在 `AliECS/` 目录下执行：

```powershell
New-Item -ItemType Directory -Force services\backend-api\app\clash_profile | Out-Null
$src = "$env:APPDATA\io.github.clash-verge-rev.clash-verge-rev\profiles\Lm0xz97pW8BX.yaml"
$lines = Get-Content $src -Encoding UTF8
$pi = ($lines | Select-String -Pattern '^proxies:').LineNumber
$ri = ($lines | Select-String -Pattern '^rules:').LineNumber
$lines[0..($pi-2)]              | Set-Content -Encoding UTF8 services\backend-api\app\clash_profile\template_base.yaml
$lines[$ri..($lines.Count - 1)] | Set-Content -Encoding UTF8 services\backend-api\app\clash_profile\template_rules.yaml
```

`$pi`、`$ri` 是 1-based 行号，所以 `$lines[0..($pi-2)]` 取到 `proxies:` 之前一行为止，`$lines[$ri..]` 从 `rules:` 的下一行开始。

- [ ] **Step 3: 核对 `template_base.yaml` 不含任何节点信息**

```powershell
Select-String -Path services\backend-api\app\clash_profile\template_base.yaml -Pattern 'uuid|password|reality|servername|vless|vmess|trojan|^proxies|^proxy-groups|^rules' -CaseSensitive:$false
```

Expected: 无任何输出。有输出说明切分位置错了，回到 Step 2 检查。

- [ ] **Step 4: 手工改 `template_rules.yaml` 两处**

改动 A —— **删掉**自建节点服务器地址的直连规则那一行（形如 `  - IP-CIDR,<地址>/32,DIRECT,no-resolve`）及其上方注释。这条改由 `render.py` 从节点定义运行时推导，留在模板里既是硬编码也是 public 仓泄漏。

改动 B —— 把下列规则的目标从 `节点选择` 改成 `AI服务`，其余规则一行不动：

```yaml
  - DOMAIN,challenges.cloudflare.com,AI服务
  - DOMAIN-SUFFIX,cloudflare.com,AI服务
  - DOMAIN,api.openai.com,AI服务
  - DOMAIN,cdn.oaistatic.com,AI服务
  - DOMAIN-SUFFIX,openai.com,AI服务
  - DOMAIN-SUFFIX,chatgpt.com,AI服务
  - DOMAIN-SUFFIX,oaistatic.com,AI服务
  - DOMAIN-SUFFIX,oaiusercontent.com,AI服务
  - DOMAIN-SUFFIX,auth.openai.com,AI服务
```

改完再核对一次不含节点地址：

```powershell
Select-String -Path services\backend-api\app\clash_profile\template_rules.yaml -Pattern 'IP-CIDR,(?!127\.|10\.|172\.16|192\.168|100\.64)' -CaseSensitive:$false
```

Expected: 无输出（只允许保留回环、内网、Tailscale 三类网段）。

- [ ] **Step 5: 写失败的测试**

Create `tests/test_clash_profile_render.py`:

```python
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def _section(rendered: str, key: str):
    """取出 `key: {json}` 这一行的 JSON 值。渲染产物里动态段都是单行 flow-style。"""
    for line in rendered.splitlines():
        if line.startswith(f"{key}: "):
            return json.loads(line[len(key) + 2 :])
    return None


class ClashProfileRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]
        sys.path.insert(0, backend_root)
        from app.clash_profile import render

        self.render = render
        self.node = {"name": "self-a", "type": "vless", "server": "203.0.113.10", "port": 443}
        self.provider = {"id": 7, "name": "机场甲", "url": "https://example.com/sub?token=x",
                         "enabled": True, "sort_order": 0}

    def tearDown(self) -> None:
        sys.path[:] = self._old_sys_path
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]

    def test_no_provider_omits_auto_group(self) -> None:
        out = self.render.render_profile([self.node], [])
        groups = _section(out, "proxy-groups")
        names = [g["name"] for g in groups]
        self.assertNotIn("自动选择", names)
        self.assertIsNone(_section(out, "proxy-providers"))
        select = next(g for g in groups if g["name"] == "节点选择")
        self.assertNotIn("use", select)

    def test_enabled_providers_only(self) -> None:
        disabled = {**self.provider, "id": 8, "name": "机场乙", "enabled": False}
        out = self.render.render_profile([self.node], [self.provider, disabled])
        providers = _section(out, "proxy-providers")
        self.assertEqual(list(providers), ["airport7"])
        self.assertEqual(providers["airport7"]["url"], "https://example.com/sub?token=x")
        self.assertEqual(providers["airport7"]["path"], "./providers/airport7.yaml")
        groups = _section(out, "proxy-groups")
        auto = next(g for g in groups if g["name"] == "自动选择")
        self.assertEqual(auto["use"], ["airport7"])

    def test_select_group_name_is_locked(self) -> None:
        # DNS 段有约 24 处 "#节点选择" 引用组名，改名会让境外 DNS 全部失效。
        out = self.render.render_profile([self.node], [self.provider])
        groups = _section(out, "proxy-groups")
        self.assertIn("节点选择", [g["name"] for g in groups])
        self.assertIn("#节点选择", out)

    def test_ipv4_server_gets_cidr32_guard(self) -> None:
        out = self.render.render_profile([self.node], [])
        self.assertIn("  - IP-CIDR,203.0.113.10/32,DIRECT,no-resolve", out.splitlines())

    def test_ipv6_server_gets_cidr128_guard(self) -> None:
        node = {**self.node, "server": "2001:db8::1"}
        out = self.render.render_profile([node], [])
        self.assertIn("  - IP-CIDR6,2001:db8::1/128,DIRECT,no-resolve", out.splitlines())

    def test_domain_server_gets_domain_guard(self) -> None:
        node = {**self.node, "server": "node.example.com"}
        out = self.render.render_profile([node], [])
        self.assertIn("  - DOMAIN,node.example.com,DIRECT", out.splitlines())

    def test_node_name_with_cjk_and_quotes_round_trips(self) -> None:
        node = {**self.node, "name": '香港"节点" 01'}
        out = self.render.render_profile([node], [])
        self.assertEqual(_section(out, "proxies")[0]["name"], '香港"节点" 01')

    def test_ai_rules_point_to_ai_group(self) -> None:
        out = self.render.render_profile([self.node], [])
        self.assertIn("  - DOMAIN-SUFFIX,openai.com,AI服务", out.splitlines())
        self.assertNotIn("  - DOMAIN-SUFFIX,openai.com,节点选择", out.splitlines())

    def test_ai_group_prefers_self_node(self) -> None:
        out = self.render.render_profile([self.node], [self.provider])
        groups = _section(out, "proxy-groups")
        ai = next(g for g in groups if g["name"] == "AI服务")
        self.assertEqual(ai["proxies"][0], "self-a")
        self.assertNotIn("use", ai)

    def test_empty_self_nodes_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.render.render_profile([], [self.provider])
```

- [ ] **Step 6: 跑测试确认失败**

Run: `python -m unittest discover -s tests -p "test_clash_profile_render.py" -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.clash_profile'`

- [ ] **Step 7: 写实现**

Create `services/backend-api/app/clash_profile/__init__.py`（空文件）。

Create `services/backend-api/app/clash_profile/render.py`:

```python
"""Clash 配置合成：静态模板 + 运行时节点定义 → 单一 YAML 文本。

不解析 YAML：静态段原样拼接，动态段用 json.dumps 生成 flow-style 值
（YAML 1.2 是 JSON 超集，mihomo 照常解析），因此本模块零第三方依赖，
且节点名里的中文、引号、emoji 由标准库正确转义。

机场节点不在这里拉取——它们由客户端 mihomo 通过 proxy-providers 自行获取
并按 interval 定期刷新，服务端完全不接触机场。
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any


_HERE = Path(__file__).resolve().parent
TEMPLATE_BASE = _HERE / "template_base.yaml"
TEMPLATE_RULES = _HERE / "template_rules.yaml"

# ⚠️ template_base.yaml 的 DNS 段有约 24 处 "#节点选择" 引用这个组名（# 后是代理组名）。
# 改名会让境外 DNS 解析全部失效，症状间歇且难排查。tests 里有断言保护。
GROUP_SELECT = "节点选择"
GROUP_AUTO = "自动选择"
GROUP_AI = "AI服务"

HEALTH_CHECK_URL = "https://www.gstatic.com/generate_204"
PROVIDER_INTERVAL = 86400
HEALTH_CHECK_INTERVAL = 300


def provider_key(provider_id: int) -> str:
    """provider 的 YAML key。用数据库 id 而非机场名，避免中文与特殊字符进 key。"""
    return f"airport{provider_id}"


def _guard_rule(server: str) -> str:
    """节点服务器地址必须直连，否则 TUN 模式下可能回环。地址来自节点定义，不硬编码。"""
    try:
        addr = ipaddress.ip_address(server)
    except ValueError:
        return f"  - DOMAIN,{server},DIRECT"
    if addr.version == 4:
        return f"  - IP-CIDR,{server}/32,DIRECT,no-resolve"
    return f"  - IP-CIDR6,{server}/128,DIRECT,no-resolve"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_profile(self_nodes: list[dict], providers: list[dict]) -> str:
    if not self_nodes:
        raise ValueError("自建节点定义为空，拒绝生成配置：AI服务 组会因此为空并导致 mihomo 启动失败")

    names = [node["name"] for node in self_nodes]
    active = sorted(
        (p for p in providers if p.get("enabled", True)),
        key=lambda p: (p.get("sort_order", 0), p["id"]),
    )
    keys = [provider_key(p["id"]) for p in active]

    parts: list[str] = [TEMPLATE_BASE.read_text(encoding="utf-8").rstrip("\n"), ""]

    parts.append("proxies: " + _dump(self_nodes))
    parts.append("")

    if active:
        parts.append("proxy-providers: " + _dump({
            provider_key(p["id"]): {
                "type": "http",
                "url": p["url"],
                "interval": PROVIDER_INTERVAL,
                "path": f"./providers/{provider_key(p['id'])}.yaml",
                "health-check": {
                    "enable": True,
                    "url": HEALTH_CHECK_URL,
                    "interval": HEALTH_CHECK_INTERVAL,
                },
            }
            for p in active
        }))
        parts.append("")

    select_group: dict[str, Any] = {"name": GROUP_SELECT, "type": "select"}
    if keys:
        select_group["proxies"] = [*names, GROUP_AUTO, "DIRECT"]
        select_group["use"] = keys
    else:
        # 空的 use 或空的 url-test 组会让 mihomo 启动失败，所以一个 provider 都没有时整段省略。
        select_group["proxies"] = [*names, "DIRECT"]

    groups: list[dict[str, Any]] = [select_group]
    if keys:
        groups.append({
            "name": GROUP_AUTO,
            "type": "url-test",
            "use": keys,
            "url": HEALTH_CHECK_URL,
            "interval": HEALTH_CHECK_INTERVAL,
            "tolerance": 50,
        })
    # AI 服务默认锁自建节点：机场共享 IP 容易触发 ChatGPT / Claude 的风控。
    groups.append({"name": GROUP_AI, "type": "select", "proxies": [*names, GROUP_SELECT]})

    parts.append("proxy-groups: " + _dump(groups))
    parts.append("")

    parts.append("rules:")
    parts.append("  # 自建节点服务器地址直连，避免回环（由节点定义推导，勿手写）")
    parts.extend(_guard_rule(node["server"]) for node in self_nodes)
    parts.append(TEMPLATE_RULES.read_text(encoding="utf-8").lstrip("\n"))

    return "\n".join(parts)
```

- [ ] **Step 8: 跑测试确认通过**

Run: `python -m unittest discover -s tests -p "test_clash_profile_render.py" -v`
Expected: 10 个用例全部 PASS

- [ ] **Step 9: 提交**

```bash
git add services/backend-api/app/clash_profile tests/test_clash_profile_render.py
git commit -m "feat(clash-profile): 配置渲染纯函数与静态模板"
```

---

### Task 2: 数据表、接口与路由挂载

**Files:**
- Create: `db/migrations/0048_clash_profile.sql`
- Create: `services/backend-api/app/routers/clash_profile.py`
- Modify: `services/backend-api/app/main.py`
- Test: `tests/test_clash_profile_router.py`

**Interfaces:**
- Consumes: Task 1 的 `render_profile(self_nodes, providers)`、`provider_key(provider_id)`
- Produces:
  - `_load_self_nodes() -> list[dict]`，从环境变量 `CLASH_SELF_NODES_JSON` 读，失败抛 `HTTPException(500)`
  - 路由前缀 `/v1/admin/clash-profile`，六个端点（见下），admin-ui 通过 `/api` 反代访问
  - 表 `clash_profile_providers(id, name, url, enabled, sort_order, created_at, updated_at)`

- [ ] **Step 1: 写迁移**

Create `db/migrations/0048_clash_profile.sql`:

```sql
-- 0048: Clash 配置合成器 —— 第三方机场订阅源
-- 设计成多行而非单行配置：机场跑路是常态，换机场时需要能先加新的、验证通过再删旧的。
-- url 含机场分配的 token，属敏感数据，只存库不进仓库。
-- 幂等：IF NOT EXISTS，可安全重复执行。
CREATE TABLE IF NOT EXISTS clash_profile_providers (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  url         TEXT NOT NULL,
  enabled     BOOLEAN NOT NULL DEFAULT TRUE,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 2: 写失败的测试**

Create `tests/test_clash_profile_router.py`:

```python
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


class ClashProfileEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        self._old_env = {k: os.environ.get(k) for k in ("AUTH_TOKEN_SECRET", "CLASH_SELF_NODES_JSON")}
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        backend_root = str(BACKEND_ROOT)
        sys.path[:] = [item for item in sys.path if item != backend_root]
        sys.path.insert(0, backend_root)
        os.environ["AUTH_TOKEN_SECRET"] = "test-clash-profile-secret"
        from app.routers import clash_profile

        self.module = clash_profile
        from fastapi import HTTPException

        self.HTTPException = HTTPException

    def tearDown(self) -> None:
        sys.path[:] = self._old_sys_path
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]

    def test_missing_env_raises_500(self) -> None:
        os.environ.pop("CLASH_SELF_NODES_JSON", None)
        with self.assertRaises(self.HTTPException) as ctx:
            self.module._load_self_nodes()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_invalid_json_raises_500(self) -> None:
        os.environ["CLASH_SELF_NODES_JSON"] = "{not json"
        with self.assertRaises(self.HTTPException) as ctx:
            self.module._load_self_nodes()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_non_list_raises_500(self) -> None:
        os.environ["CLASH_SELF_NODES_JSON"] = '{"name": "x"}'
        with self.assertRaises(self.HTTPException) as ctx:
            self.module._load_self_nodes()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_empty_list_raises_500(self) -> None:
        os.environ["CLASH_SELF_NODES_JSON"] = "[]"
        with self.assertRaises(self.HTTPException) as ctx:
            self.module._load_self_nodes()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_node_without_required_keys_raises_500(self) -> None:
        os.environ["CLASH_SELF_NODES_JSON"] = '[{"name": "a"}]'
        with self.assertRaises(self.HTTPException) as ctx:
            self.module._load_self_nodes()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_valid_env_returns_nodes(self) -> None:
        os.environ["CLASH_SELF_NODES_JSON"] = '[{"name": "a", "server": "203.0.113.10", "type": "vless"}]'
        nodes = self.module._load_self_nodes()
        self.assertEqual(nodes[0]["server"], "203.0.113.10")

    def test_router_prefix_is_admin_scoped(self) -> None:
        self.assertEqual(self.module.router.prefix, "/v1/admin/clash-profile")
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m unittest discover -s tests -p "test_clash_profile_router.py" -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.routers.clash_profile'`

- [ ] **Step 4: 写路由**

Create `services/backend-api/app/routers/clash_profile.py`:

```python
"""Clash 配置合成：第三方机场订阅源的增删改，以及合成配置的预览与下载。

自建节点定义来自环境变量 CLASH_SELF_NODES_JSON（由 SOPS 管理、部署时渲染），
仓库里没有也不得有。机场订阅 URL 存库，不进仓库。

本模块不访问机场——机场节点由客户端 mihomo 通过 proxy-providers 自行拉取。
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.clash_profile.render import render_profile
from app.core import _conn, require_admin


router = APIRouter(prefix="/v1/admin/clash-profile", tags=["clash-profile"])

_COLUMNS = "id, name, url, enabled, sort_order"


class ProviderIn(BaseModel):
    name: str
    url: str
    enabled: bool = True
    sort_order: int = 0


def _load_self_nodes() -> list[dict[str, Any]]:
    raw = os.getenv("CLASH_SELF_NODES_JSON", "").strip()
    if not raw:
        raise HTTPException(status_code=500, detail="CLASH_SELF_NODES_JSON 未配置，无法生成配置")
    try:
        nodes = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"CLASH_SELF_NODES_JSON 不是合法 JSON：{exc}") from exc
    if not isinstance(nodes, list) or not nodes:
        raise HTTPException(status_code=500, detail="CLASH_SELF_NODES_JSON 必须是非空数组")
    for node in nodes:
        if not isinstance(node, dict) or "name" not in node or "server" not in node:
            raise HTTPException(status_code=500, detail="CLASH_SELF_NODES_JSON 的每个元素都必须含 name 与 server")
    return nodes


def _rows() -> list[dict[str, Any]]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM clash_profile_providers ORDER BY sort_order, id")
            return [
                {"id": r[0], "name": r[1], "url": r[2], "enabled": r[3], "sort_order": r[4]}
                for r in cur.fetchall()
            ]


def _profile_text() -> str:
    try:
        return render_profile(_load_self_nodes(), _rows())
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/providers")
def list_providers(_: dict = Depends(require_admin)) -> dict[str, Any]:
    return {"items": _rows()}


@router.post("/providers", status_code=201)
def create_provider(payload: ProviderIn, _: dict = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clash_profile_providers(name, url, enabled, sort_order)"
                f" VALUES (%s, %s, %s, %s) RETURNING {_COLUMNS}",
                (payload.name, payload.url, payload.enabled, payload.sort_order),
            )
            row = cur.fetchone()
        conn.commit()
    return {"id": row[0], "name": row[1], "url": row[2], "enabled": row[3], "sort_order": row[4]}


@router.put("/providers/{provider_id}")
def update_provider(provider_id: int, payload: ProviderIn, _: dict = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE clash_profile_providers"
                " SET name = %s, url = %s, enabled = %s, sort_order = %s, updated_at = now()"
                f" WHERE id = %s RETURNING {_COLUMNS}",
                (payload.name, payload.url, payload.enabled, payload.sort_order, provider_id),
            )
            row = cur.fetchone()
        conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="订阅源不存在")
    return {"id": row[0], "name": row[1], "url": row[2], "enabled": row[3], "sort_order": row[4]}


@router.delete("/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: int, _: dict = Depends(require_admin)) -> None:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM clash_profile_providers WHERE id = %s", (provider_id,))
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="订阅源不存在")


@router.get("/preview", response_class=PlainTextResponse)
def preview_profile(_: dict = Depends(require_admin)) -> PlainTextResponse:
    return PlainTextResponse(_profile_text(), media_type="text/plain; charset=utf-8")


@router.get("/download", response_class=PlainTextResponse)
def download_profile(_: dict = Depends(require_admin)) -> PlainTextResponse:
    return PlainTextResponse(
        _profile_text(),
        media_type="text/yaml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="clash-profile.yaml"'},
    )
```

- [ ] **Step 5: 挂载路由**

Modify `services/backend-api/app/main.py` —— 在 import 区按现有位置加一行（`backups` 之后、`versions` 之前保持字母无关的既有风格即可）：

```python
from app.routers.clash_profile import router as clash_profile_router
```

在 `app.include_router(backups_router)` 之后加一行：

```python
app.include_router(clash_profile_router)
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m unittest discover -s tests -p "test_clash_profile_router.py" -v`
Expected: 7 个用例全部 PASS

- [ ] **Step 7: 跑全量测试确认没打破别的**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add db/migrations/0048_clash_profile.sql services/backend-api/app/routers/clash_profile.py services/backend-api/app/main.py tests/test_clash_profile_router.py
git commit -m "feat(clash-profile): 订阅源表、管理接口与配置下载"
```

---

### Task 3: admin-ui 页签

**Files:**
- Modify: `services/admin-ui/index.html`
- Test: `tests/test_admin_frontend.py`（追加一个 TestCase 方法）

**Interfaces:**
- Consumes: Task 2 的六个端点，经 admin-ui 既有的 `api(path, options)` 辅助函数调用，路径以 `/v1/admin/clash-profile` 开头
- Produces: 无（终端 UI）

admin-ui 是单文件页面：`nav#secNav` 里一个 `<button data-target="...">`，对应一个 `<details class="card" id="...">` 区块，JS 侧在 `state` 里放数据、写一个 `loadXxx()` 和一个 `renderXxx()`。照这个模式来，不要另起风格。

- [ ] **Step 1: 写失败的测试**

Modify `tests/test_admin_frontend.py`，在 `AdminFrontendTests` 类里追加：

```python
    def test_clash_profile_panel_is_present(self) -> None:
        self.assertIn('data-target="sec-clash-profile"', self.html)
        self.assertIn('id="sec-clash-profile"', self.html)
        self.assertIn('id="clashProviderBody"', self.html)
        self.assertIn('id="clashDownloadBtn"', self.html)
        self.assertIn('id="clashCopyBtn"', self.html)
        self.assertIn('api("/v1/admin/clash-profile/providers")', self.html)
        self.assertIn("async function loadClashProviders()", self.html)
        self.assertIn("function renderClashProviders()", self.html)

    def test_clash_profile_urls_are_masked_by_default(self) -> None:
        # 订阅 URL 含机场分配的 token，列表默认打码，点开才显示。
        self.assertIn("function maskSubscriptionUrl(", self.html)
        self.assertIn("clashProfile:{items:[],revealed:{}}", self.html)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest discover -s tests -p "test_admin_frontend.py" -v`
Expected: 新增的两个用例 FAIL，其余 PASS

- [ ] **Step 3: 加导航按钮**

Modify `services/admin-ui/index.html`，在 `<nav id="secNav" class="secnav">` 里 `systemConfigPanel` 那行之前插入：

```html
        <button type="button" data-target="sec-clash-profile">Clash 配置</button>
```

- [ ] **Step 4: 加面板区块**

在 `sec-features` 区块的 `</details>` 之后插入：

```html
      <!-- Clash 配置合成 -->
      <details class="card" id="sec-clash-profile" data-sec="clash-profile">
        <summary><h2><span class="htitle">Clash 配置 <span id="clashProviderCountBadge" class="badge">0</span></span></h2></summary>
        <div class="notice">
          机场节点由客户端自行定期同步，增删节点无需重新导入配置。
          只有更换机场、改动自建节点或调整分流规则后，才需要重新下载并在各客户端重新导入。
        </div>
        <div class="sec-tools">
          <button id="toggleClashProviderForm" type="button">＋ 新增订阅源</button>
          <button id="clashDownloadBtn" type="button">下载配置</button>
          <button id="clashCopyBtn" type="button">复制配置文本</button>
        </div>
        <div id="clashProviderForm" class="collapse hidden">
          <div class="grid two">
            <div><label>名称</label><input id="clashProviderName" placeholder="仅用于本页显示" /></div>
            <div><label>排序</label><input id="clashProviderSort" type="number" value="0" /></div>
            <div style="grid-column:1/-1"><label>订阅 URL</label><input id="clashProviderUrl" placeholder="https://..." /></div>
          </div>
          <div class="row" style="margin-top:14px;"><button id="createClashProviderBtn" class="primary" type="button">新增</button></div>
        </div>
        <div id="clashProviderBody" class="entity-grid"></div>
      </details>
```

- [ ] **Step 5: 加 state 与 JS**

在 `state` 对象里追加一个字段（与 `auditLogs:{...}` 同级）：

```javascript
      clashProfile:{items:[],revealed:{}},
```

在 `loadAuditLogs` 一类的函数附近追加：

```javascript
    function maskSubscriptionUrl(url){
      const raw=String(url||"");
      if(raw.length<=24) return "••••";
      return `${raw.slice(0,16)}••••${raw.slice(-6)}`;
    }
    async function loadClashProviders(){
      const resp=await api("/v1/admin/clash-profile/providers");
      state.clashProfile.items=resp.items||[];
      renderClashProviders();
    }
    function renderClashProviders(){
      const items=state.clashProfile.items;
      $("clashProviderCountBadge").textContent=items.length;
      $("clashProviderBody").innerHTML=items.map(p=>{
        const shown=state.clashProfile.revealed[p.id]?escapeHtml(p.url):escapeHtml(maskSubscriptionUrl(p.url));
        return `<div class="entity">
          <div><strong>${escapeHtml(p.name)}</strong> ${p.enabled?"":'<span class="badge">已停用</span>'}</div>
          <div class="muted" style="word-break:break-all">${shown}</div>
          <div class="row">
            <button type="button" data-clash-reveal="${p.id}">${state.clashProfile.revealed[p.id]?"隐藏":"显示"} URL</button>
            <button type="button" data-clash-toggle="${p.id}">${p.enabled?"停用":"启用"}</button>
            <button type="button" data-clash-delete="${p.id}">删除</button>
          </div>
        </div>`;
      }).join("")||'<div class="muted">还没有订阅源。</div>';
    }
    async function createClashProvider(){
      const name=$("clashProviderName").value.trim();
      const url=$("clashProviderUrl").value.trim();
      if(!name||!url){showError("名称与订阅 URL 都不能为空。");return;}
      await api("/v1/admin/clash-profile/providers",{method:"POST",body:JSON.stringify({name,url,enabled:true,sort_order:Number($("clashProviderSort").value||0)})});
      $("clashProviderName").value="";$("clashProviderUrl").value="";
      showSuccess("订阅源已新增。");
      await loadClashProviders();
    }
    async function toggleClashProvider(id){
      const item=state.clashProfile.items.find(p=>String(p.id)===String(id));
      if(!item) return;
      await api(`/v1/admin/clash-profile/providers/${id}`,{method:"PUT",body:JSON.stringify({name:item.name,url:item.url,enabled:!item.enabled,sort_order:item.sort_order})});
      await loadClashProviders();
    }
    async function deleteClashProvider(id){
      if(!confirm("确认删除这个订阅源？")) return;
      await api(`/v1/admin/clash-profile/providers/${id}`,{method:"DELETE"});
      showSuccess("订阅源已删除。");
      await loadClashProviders();
    }
    async function fetchClashProfileText(){
      const resp=await fetch(`${API_BASE}/v1/admin/clash-profile/preview`,{headers:{Authorization:`Bearer ${state.token}`}});
      if(!resp.ok) throw new Error(await resp.text());
      return await resp.text();
    }
    async function downloadClashProfile(){
      try{
        const text=await fetchClashProfileText();
        const blob=new Blob([text],{type:"text/yaml;charset=utf-8"});
        const a=document.createElement("a");
        a.href=URL.createObjectURL(blob);a.download="clash-profile.yaml";a.click();
        URL.revokeObjectURL(a.href);
      }catch(err){showError(`生成配置失败：${err.message}`);}
    }
    async function copyClashProfile(){
      try{
        await navigator.clipboard.writeText(await fetchClashProfileText());
        showSuccess("配置文本已复制。");
      }catch(err){showError(`复制失败：${err.message}`);}
    }
```

上面用到的辅助函数在 `index.html` 里都已存在，已逐个核对，直接用即可，不要新造：

| 名称 | 定义 |
|---|---|
| `$` | `const $ = (id) => document.getElementById(id);` |
| `showError` / `showSuccess` | 包装 `AliECSToast.show(m, "error"/"success")` |
| `escapeHtml` | 已有，`renderSafeLink` 在用 |
| `state.token` | 由 `syncTokenState()` 从 localStorage 同步，`api()` 用它拼 `Authorization: Bearer` |
| `API_BASE` | `location.port === "8081" ? "http://localhost:8000" : "/api"` |

`api()` 会把响应体当 JSON 解析，所以返回纯文本的 `/preview` 必须用上面那样的原生 `fetch`，不能走 `api()`。

- [ ] **Step 6: 接事件与初始加载**

在现有的事件绑定区追加：

```javascript
    $("toggleClashProviderForm").addEventListener("click",()=>$("clashProviderForm").classList.toggle("hidden"));
    $("createClashProviderBtn").addEventListener("click",createClashProvider);
    $("clashDownloadBtn").addEventListener("click",downloadClashProfile);
    $("clashCopyBtn").addEventListener("click",copyClashProfile);
    $("clashProviderBody").addEventListener("click",(e)=>{
      const reveal=e.target.getAttribute("data-clash-reveal");
      if(reveal){state.clashProfile.revealed[reveal]=!state.clashProfile.revealed[reveal];renderClashProviders();return;}
      const toggle=e.target.getAttribute("data-clash-toggle");
      if(toggle){toggleClashProvider(toggle);return;}
      const del=e.target.getAttribute("data-clash-delete");
      if(del){deleteClashProvider(del);}
    });
```

在 `loadAll()` 里追加一次加载：

```javascript
      await loadClashProviders();
```

- [ ] **Step 7: 跑测试确认通过**

Run: `python -m unittest discover -s tests -p "test_admin_frontend.py" -v`
Expected: 全部 PASS

- [ ] **Step 8: 检查内联脚本语法**

AGENTS.md 要求前端改动必须做 JS 语法检查。把 `<script>` 块内容抽出来跑一次语法解析：

```powershell
$html = Get-Content services\admin-ui\index.html -Raw
$m = [regex]::Match($html, '(?s)<script>(.*)</script>')
$m.Groups[1].Value | Set-Content -Encoding UTF8 $env:TEMP\admin-ui-check.js
node --check $env:TEMP\admin-ui-check.js
```

Expected: 无输出即通过。若本机没有 node，改用浏览器打开页面并看 Console 无报错。

- [ ] **Step 9: 提交**

```bash
git add services/admin-ui/index.html tests/test_admin_frontend.py
git commit -m "feat(clash-profile): admin-ui 订阅源管理与配置下载页签"
```

---

### Task 4: 环境变量示例与文档闭环

**Files:**
- Modify: `local/.env.local.example`
- Modify: `deploy/ecs/runtime.env.example`
- Modify: `services/backend-api/README.md`
- Modify: `docs/project-navigation.md`
- Modify: `docs/project-ai-map.md`
- Modify: `AGENTS.md`
- Modify: 顶层 `../功能地图-人类版.md`（workspace 治理仓，**单独提交**）

**Interfaces:**
- Consumes: Task 2 定义的环境变量名 `CLASH_SELF_NODES_JSON`
- Produces: 无

- [ ] **Step 1: 两个 env 示例各加一行**

在 `local/.env.local.example` 与 `deploy/ecs/runtime.env.example` 末尾各追加：

```bash
# Clash 配置合成器：自建节点定义，JSON 数组，每个元素是一个完整的 clash proxy 定义（至少含 name 与 server）。
# 生产值由 SOPS 管理并在部署时渲染，切勿把真实节点参数写进仓库。
CLASH_SELF_NODES_JSON=[{"name":"example","type":"vless","server":"203.0.113.10","port":443}]
```

- [ ] **Step 2: backend-api README 记录新增环境变量与接口**

在 `services/backend-api/README.md` 的环境变量小节追加 `CLASH_SELF_NODES_JSON` 的用途、格式与「缺失时 `/download` 返回 500」的行为；在接口小节追加 `/v1/admin/clash-profile` 六个端点。

- [ ] **Step 3: 导航文档记录功能入口**

`docs/project-navigation.md` 与 `docs/project-ai-map.md` 各加一条：人类叫法「Clash 配置 / 订阅合并」→ 代码位置 `services/backend-api/app/clash_profile/` + `services/backend-api/app/routers/clash_profile.py` + admin-ui `sec-clash-profile` 区块；验证命令 `python -m unittest discover -s tests -p "test_clash_profile_render.py"`。

- [ ] **Step 4: AGENTS.md 增加 public 仓约束**

在 `AGENTS.md` 的「关键边界」小节追加：

```markdown
**本仓是 PUBLIC 仓库。** 提交任何内容前先确认它可以公开：代理节点的地址/端口/传输参数/凭据、
第三方服务的订阅 URL 与账号标识、内网拓扑细节，一律走环境变量（SOPS 渲染）或数据库，
不进仓库、不进注释、不进测试 fixture、不进设计文档。测试一律用 RFC 5737 文档地址等占位值。
```

这条是这次实施中差点踩空的地方——`gh repo view huozao/AliECS` 显示 `PUBLIC`，而顶层仓库表格只给 `infra` 和 `material_rnd` 标了私有，容易误判。

- [ ] **Step 5: 跑全量测试**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 6: 提交（AliECS 仓）**

```bash
git add local/.env.local.example deploy/ecs/runtime.env.example services/backend-api/README.md docs/project-navigation.md docs/project-ai-map.md AGENTS.md
git commit -m "docs(clash-profile): 环境变量示例、导航闭环与 public 仓约束"
```

- [ ] **Step 7: 顶层功能地图（workspace 治理仓，单独提交）**

⚠️ 跨仓库，必须切到工作区根目录单独提交，不得与 AliECS 的提交混在一起。

```bash
cd ..
git add 功能地图-人类版.md
git commit -m "docs: 登记 Clash 配置合成器功能入口"
cd AliECS
```

---

### Task 5: SOPS 密钥与部署（需用户逐步授权）

**Files:**
- Modify: infra 仓 `secrets/` 下对应的 SOPS 文件
- Modify: infra 仓 `secrets/README.md`

**Interfaces:**
- Consumes: Task 2 的环境变量名 `CLASH_SELF_NODES_JSON`
- Produces: 生产环境可用的自建节点定义

⚠️ **本任务跨仓库且涉及密钥与生产部署，每一步都要先向用户报告再执行，不得自动推进。**

- [ ] **Step 1: 从 devbox 现用配置取出自建节点定义**

devbox 的 `%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\profiles\Lm0xz97pW8BX.yaml` 里 `proxies:` 段就是要的内容，把那一个 YAML 节点手工转成 JSON 数组的一个元素。**这个值含 UUID 等凭据，只在 SOPS 里落地，不得写进任何文件、日志或对话记录。**

- [ ] **Step 2: 写入 SOPS**

按 infra `AGENTS.md` 的流程：devbox `sops set` → push origin → push 各 device bare → 设备侧 `render.sh` → 重建容器（`restart` 不重读 env_file，必须 force-recreate）。

Windows 上执行 sops 需要 `SOPS_AGE_KEY_FILE`。

- [ ] **Step 3: infra 仓 secrets/README.md 登记新键**

- [ ] **Step 4: 开 PR 并部署 AliECS**

按 `docs/runbooks/deploy.md` 闭环。判据是 `stage-business-cn-peer`，`deploy-business-cn` 恒为 skipped——只看 job 成功不够，还要看容器实际起来。

---

### Task 6: 生成配置并验收（手工，单测覆盖不到）

**Files:** 无（验证任务）

**Interfaces:**
- Consumes: Task 5 部署完成的线上服务
- Produces: 验收结论

- [ ] **Step 1: 配置语法校验**

从 admin-ui 下载配置，用 Clash Verge 自带的 mihomo 核心做语法校验。这一步比任何单测都硬，能抓出拼接产生的缩进与结构错误：

```powershell
$core = Get-ChildItem "$env:LOCALAPPDATA\io.github.clash-verge-rev.clash-verge-rev" -Recurse -Filter "verge-mihomo*.exe" | Select-Object -First 1
& $core.FullName -t -f "$env:USERPROFILE\Downloads\clash-profile.yaml"
```

Expected: 输出配置校验通过，无 error。若核心不在该路径，从 Clash Verge 安装目录找 `verge-mihomo.exe`。

- [ ] **Step 2: Windows Clash Verge 导入**

导入为新 profile 后确认：自建节点与机场节点都出现在 `节点选择` 组；机场的流量用量与到期时间正常显示（mihomo 会解析 provider 响应的 `subscription-userinfo` 头）。

- [ ] **Step 3: 机场节点连通性**

切到一个机场节点访问境外站点。**重点验这个**：自建节点有运行时推导的防回环规则保护，机场节点的服务器地址来自 provider、动态未知，无法预生成同类规则，只能依赖 TUN 模式下 mihomo 的 `auto-route` + `auto-detect-interface` 自动绕过自身代理连接。这是设计文档里列出的已知风险之一。

- [ ] **Step 4: DNS 未回归**

切到机场节点后访问 YouTube 等依赖 `nameserver-policy` 的站点。若解析异常，优先怀疑 `节点选择` 组名被改动——DNS 段有约 24 处 `#节点选择` 引用。

- [ ] **Step 5: AI 服务锁定**

确认 ChatGPT 走的是自建节点而非机场节点（Clash Verge 连接面板里看实际出站）。

- [ ] **Step 6: Android FlClash 导入**

同一份配置导入 FlClash。确认：能正常启动；FlClash 自身的 TUN 开关与模板里 `tun.enable: true` 不冲突；首次导入若 `GEOSITE,CN` 规则报错，需要先手选一个节点连上再下载 geodata。

- [ ] **Step 7: 回归——两个 profile 可以退休**

确认新配置完全覆盖原来两个 profile 的能力后，才在 Clash Verge 里停用旧的本地 profile 与机场 remote profile。**先停用不删除**，观察几天。

---

## 自查

**Spec 覆盖**：设计文档的每一节都有对应任务——关键决策（Task 1 的架构）、安全约束（Global Constraints + Task 4 Step 4）、三处改动（Task 1 Step 4 + Step 7）、实现文件清单（Task 1/2/3）、数据表（Task 2 Step 1）、渲染逻辑（Task 1 Step 7）、环境变量（Task 2 + Task 4 Step 1）、接口（Task 2 Step 4）、页面（Task 3）、使用流程（Task 3 Step 4 的 notice 文案）、测试八条（Task 1 Step 5，实际写了 10 条）、手工验证六步（Task 6）、已知风险（Task 6 Step 3/6）、文档闭环（Task 4）。

**类型一致性**：`render_profile(self_nodes, providers)` 在 Task 1 定义、Task 2 调用，参数顺序与键名一致；`provider_key(id)` 返回的 `airport{id}` 在 Task 1 实现与测试断言中一致；`_load_self_nodes()` 在 Task 2 定义并被同任务的 `_profile_text()` 调用；admin-ui 调用的六个路径与 Task 2 的路由前缀 `/v1/admin/clash-profile` 一致。

**外部依赖核对**：Task 3 用到的 admin-ui 辅助函数（`$`、`showError`、`showSuccess`、`escapeHtml`、`state.token`、`API_BASE`）已逐个在 `index.html` 中确认存在，签名见 Task 3 Step 5 的表。Task 1 用到的模板抽取源文件为 devbox 本机 Clash Verge 现用 profile，路径见 Step 2。`services/backend-api/Dockerfile` 是 `COPY app ./app`，两个模板在 `app/clash_profile/` 下会被一并打包，无需改 Dockerfile。
