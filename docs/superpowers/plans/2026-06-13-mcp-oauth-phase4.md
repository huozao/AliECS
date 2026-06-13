# MCP Coding Route Phase 4: OAuth 2.1 自托管授权 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `services/mcp-coding-server` 加 OAuth 2.1（自托管授权服务器 + 资源服务器），使 ChatGPT 连接器调用任何 MCP 工具前必须持有有效 token；单口令同意页 + 持久化（部署后免重授权）。这是把 Phase 3a 写工具接入连接器前的硬前置。

**Architecture:** 用 `mcp` SDK（1.27.x，已随 `mcp>=1.9,<2` 解析安装）内置的 OAuth 能力：实现 `OAuthAuthorizationServerProvider`（9 个方法）+ `AuthSettings`，由 `FastMCP(auth_server_provider=..., auth=...)` 自动挂载 `/authorize`、`/token`、`/register`、`/revoke` 与元数据端点。用户认证 = 自托管单口令同意页（`/oauth/consent`）。所有 client/code/token 落 **SQLite**（opaque token 以 `SHA-256(pepper+token)` 存储，原始 token 只在 ChatGPT 侧）。`/healthz` 与同意页保持公开；Nginx 秘密路径保留作纵深防御。

**Tech Stack:** Python stdlib（`sqlite3`/`secrets`/`hashlib`/`hmac`/`json`），`mcp.server.auth`（FastMCP 内置 OAuth），Starlette（随 mcp），`unittest` + 现有 importlib 文件加载法（规避 `app` 包名冲突）。

---

## 关键事实（已用安装版核实，写代码依据）

- `mcp` 解析为 **1.27.2**；`mcp` 仅要求 `starlette>=0.27`（无上限），与后端 `fastapi`(`starlette<0.48`) **同一 venv 无冲突**，**无需拆 CI**。
- `FastMCP.__init__` 接受 `auth_server_provider`、`token_verifier`、`auth`(=`AuthSettings`)。传 `auth_server_provider`+`auth` 时 FastMCP 自动用 `ProviderTokenVerifier(provider)` 校验工具调用的 Bearer token，并挂上 `/authorize`、`/token`、`/register`、`/revoke`、`/.well-known/oauth-authorization-server`、`/.well-known/oauth-protected-resource`。
- **PKCE 由 SDK `TokenHandler` 校验**（`mcp/server/auth/handlers/token.py`：对 `code_verifier` 做 sha256 与 `auth_code.code_challenge` 比对）。本 provider **只存 `code_challenge`，绝不自己验 PKCE**。
- Provider 9 方法签名（全部 async，除标注）：
  - `authorize(client, params: AuthorizationParams) -> str`（返回浏览器要跳转的 URL）
  - `register_client(client_info: OAuthClientInformationFull) -> None`
  - `get_client(client_id) -> OAuthClientInformationFull | None`
  - `load_authorization_code(client, code: str) -> AuthorizationCode | None`
  - `exchange_authorization_code(client, authorization_code: AuthorizationCode) -> OAuthToken`
  - `load_refresh_token(client, refresh_token: str) -> RefreshToken | None`
  - `exchange_refresh_token(client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken`
  - `load_access_token(token: str) -> AccessToken | None`
  - `revoke_token(token) -> None`
- 类型字段：`AuthorizationParams`(state, scopes, code_challenge, redirect_uri, redirect_uri_provided_explicitly, resource)；`AuthorizationCode`(code, scopes, expires_at, client_id, code_challenge, redirect_uri, redirect_uri_provided_explicitly, resource, subject)；`AccessToken`(token, client_id, scopes, expires_at, resource, subject, claims)；`RefreshToken`(token, client_id, scopes, expires_at, subject)；`OAuthToken`(access_token, token_type, expires_in, scope, refresh_token)。
- 导入路径：`from mcp.server.auth.provider import OAuthAuthorizationServerProvider, AuthorizationParams, AuthorizationCode, AccessToken, RefreshToken, construct_redirect_uri`；`from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions`；`from mcp.shared.auth import OAuthClientInformationFull, OAuthToken`。

## 相对 spec 的细化（请知悉）

1. **token 模型**：spec 写的是 JWT/无状态；改为 **opaque + SQLite 存储**（更贴合 SDK 的 `load_*` 查表契约、撤销/轮换更简单、随 SQLite 持久化）。
2. `MCP_OAUTH_SIGNING_SECRET` **重定位为「pepper」**：DB 里只存 `SHA-256(pepper + 原始值)`，原始 token/code 不落盘。你已生成的那个值直接用，无需重生成。
3. 单作用域 `coding`；`MCP_OAUTH_ENABLED` 灰度开关（默认关 → 现有行为/测试不需 token；prod 置真）。

## File Structure

| 文件 | 职责 |
|---|---|
| `services/mcp-coding-server/app/oauth/__init__.py` | 包标记（空） |
| `services/mcp-coding-server/app/oauth/config.py` | 读环境变量 → `OAuthConfig` |
| `services/mcp-coding-server/app/oauth/store.py` | SQLite 持久化（clients/pending/codes/tokens，pepper 哈希） |
| `services/mcp-coding-server/app/oauth/provider.py` | `AliecsOAuthProvider`（9 方法 + `complete_authorization`） |
| `services/mcp-coding-server/app/oauth/consent.py` | 单口令同意页路由（GET 表单 / POST 校验） |
| `services/mcp-coding-server/app/main.py` | 按开关装配 provider+AuthSettings+consent 路由（修改） |
| `services/mcp-coding-server/requirements.txt` | `mcp>=1.27,<2`（修改） |
| `deploy/ecs/runtime.env.example` | 新增 `MCP_OAUTH_*` 占位（修改） |
| `services/mcp-coding-server/README.md` | 部署/人工步骤（修改或新建） |
| `tests/test_mcp_oauth_store.py` / `_provider.py` / `_consent.py` / `_integration.py` | 测试 |

测试统一用此加载器（规避 `app` 包名冲突，仿 `tests/test_mcp_coding_server.py`）：

```python
import importlib.util, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVC = ROOT / "services" / "mcp-coding-server" / "app"
_PKG = "mcp_oauth_pkg"

def load_oauth():
    spec = importlib.util.spec_from_file_location(_PKG, SVC / "__init__.py", submodule_search_locations=[str(SVC)])
    pkg = importlib.util.module_from_spec(spec); sys.modules[_PKG] = pkg; spec.loader.exec_module(pkg)
    sub = importlib.util.spec_from_file_location(f"{_PKG}.oauth", SVC / "oauth" / "__init__.py",
                                                 submodule_search_locations=[str(SVC / "oauth")])
    oauth = importlib.util.module_from_spec(sub); sys.modules[f"{_PKG}.oauth"] = oauth; sub.loader.exec_module(oauth)
    mods = {}
    for name in ("config", "store", "provider", "consent"):
        s = importlib.util.spec_from_file_location(f"{_PKG}.oauth.{name}", SVC / "oauth" / f"{name}.py")
        m = importlib.util.module_from_spec(s); sys.modules[f"{_PKG}.oauth.{name}"] = m; s.loader.exec_module(m)
        mods[name] = m
    return mods
```

---

## Task 1: 依赖、环境占位与 compose 接线

**Files:**
- Modify: `services/mcp-coding-server/requirements.txt`
- Modify: `deploy/ecs/runtime.env.example`
- Modify: `deploy/ecs/compose.prod.yml`

> 注：`compose.prod.yml` 里 `mcp-coding-server` 用的是**显式 `environment:` 列表**（非 `env_file`），所以 `MCP_OAUTH_*` 必须显式透传；持久化用命名卷 `mcp_oauth_data`（仿现有 `tplus_sync_data`）。Nginx 在 ECS 主机上、不在仓库，属激活期人工步骤（Task 7）。

- [ ] **Step 1: pin mcp 到含 OAuth 的版本**

把 `services/mcp-coding-server/requirements.txt` 的 `mcp>=1.9,<2` 改为：

```
mcp>=1.27,<2
```

保留 `starlette<0.48`（与后端共享 venv 兼容；mcp 仅需 starlette>=0.27）。

- [ ] **Step 2: 校验本地已满足**

Run: `python -c "from importlib.metadata import version; print(version('mcp'))"`
Expected: `1.27.2`（或更高 1.x）。

- [ ] **Step 3: 加环境变量占位**

在 `deploy/ecs/runtime.env.example` 末尾追加（**仅占位，真实值在 ECS runtime.env、不进 git**）：

```
# --- MCP coding OAuth (Phase 4) ---
MCP_OAUTH_ENABLED=false
MCP_OAUTH_ISSUER=
MCP_OAUTH_PASSPHRASE=
MCP_OAUTH_SIGNING_SECRET=
MCP_OAUTH_STORE_PATH=/data/oauth/oauth.db
MCP_OAUTH_ACCESS_TTL=3600
MCP_OAUTH_REFRESH_TTL=2592000
MCP_OAUTH_CODE_TTL=600
```

- [ ] **Step 4: compose 透传 OAuth 环境变量 + 挂持久化卷**

在 `deploy/ecs/compose.prod.yml` 的 `mcp-coding-server:` 服务：
(1) `environment:` 块内（紧随 `EXECUTOR_TIMEOUT_SECONDS` 之后）追加：

```yaml
      MCP_OAUTH_ENABLED: ${MCP_OAUTH_ENABLED:-false}
      MCP_OAUTH_ISSUER: ${MCP_OAUTH_ISSUER:-}
      MCP_OAUTH_PASSPHRASE: ${MCP_OAUTH_PASSPHRASE:-}
      MCP_OAUTH_SIGNING_SECRET: ${MCP_OAUTH_SIGNING_SECRET:-}
      MCP_OAUTH_STORE_PATH: ${MCP_OAUTH_STORE_PATH:-/data/oauth/oauth.db}
      MCP_OAUTH_ACCESS_TTL: ${MCP_OAUTH_ACCESS_TTL:-3600}
      MCP_OAUTH_REFRESH_TTL: ${MCP_OAUTH_REFRESH_TTL:-2592000}
      MCP_OAUTH_CODE_TTL: ${MCP_OAUTH_CODE_TTL:-600}
```

(2) 给该服务追加 `volumes:`（与其 `extra_hosts:` 同级缩进）：

```yaml
    volumes:
      - mcp_oauth_data:/data/oauth
```

(3) 在文件顶层 `volumes:` 段登记命名卷（仿现有条目）：

```yaml
  mcp_oauth_data:
```

- [ ] **Step 5: 校验 compose 语法**

Run（本机有 docker 时）：`docker compose --env-file deploy/ecs/runtime.env.example -f deploy/ecs/compose.prod.yml config > $null`
Expected: 无报错。本机无 docker 则跳过——CI 的 `validate` job 会跑同样的 prod compose 校验兜底。

- [ ] **Step 6: Commit**

```bash
git add services/mcp-coding-server/requirements.txt deploy/ecs/runtime.env.example deploy/ecs/compose.prod.yml
git commit -m "chore(mcp-oauth): pin mcp>=1.27, add MCP_OAUTH_* env + compose passthrough/volume"
```

## Task 2: `oauth/config.py`

**Files:**
- Create: `services/mcp-coding-server/app/oauth/__init__.py`（空文件）
- Create: `services/mcp-coding-server/app/oauth/config.py`
- Test: `tests/test_mcp_oauth_config.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_mcp_oauth_config.py
from __future__ import annotations
import os, unittest
from unittest import mock
# (粘贴 File Structure 里的 load_oauth() 加载器)

class ConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        cfg = load_oauth()["config"]
        with mock.patch.dict(os.environ, {}, clear=True):
            c = cfg.config_from_env()
        self.assertFalse(c.enabled)

    def test_enabled_and_fields(self):
        cfg = load_oauth()["config"]
        env = {"MCP_OAUTH_ENABLED": "true", "MCP_OAUTH_ISSUER": "https://h.xyz/mcp-abc/",
               "MCP_OAUTH_PASSPHRASE": "pw", "MCP_OAUTH_SIGNING_SECRET": "x"*32,
               "MCP_OAUTH_STORE_PATH": "/tmp/x.db", "MCP_OAUTH_ACCESS_TTL": "120"}
        with mock.patch.dict(os.environ, env, clear=True):
            c = cfg.config_from_env()
        self.assertTrue(c.enabled)
        self.assertTrue(c.fully_configured)
        self.assertEqual(c.issuer_url, "https://h.xyz/mcp-abc")  # 去尾斜杠
        self.assertEqual(c.access_ttl, 120)
```

- [ ] **Step 2: 运行（应失败：模块不存在）**

Run: `python -m unittest discover -s tests -p test_mcp_oauth_config.py -v`
Expected: FAIL（ModuleNotFoundError/AttributeError）。

- [ ] **Step 3: 实现**

```python
# services/mcp-coding-server/app/oauth/__init__.py
```
（空文件）

```python
# services/mcp-coding-server/app/oauth/config.py
from __future__ import annotations
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class OAuthConfig:
    enabled: bool
    issuer_url: str
    passphrase: str
    pepper: str
    store_path: str
    scope: str = "coding"
    access_ttl: int = 3600
    refresh_ttl: int = 30 * 24 * 3600
    code_ttl: int = 600
    txn_ttl: int = 600

    @property
    def fully_configured(self) -> bool:
        return bool(self.issuer_url and self.passphrase and self.pepper and self.store_path)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def config_from_env() -> OAuthConfig:
    enabled = os.getenv("MCP_OAUTH_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    return OAuthConfig(
        enabled=enabled,
        issuer_url=os.getenv("MCP_OAUTH_ISSUER", "").strip().rstrip("/"),
        passphrase=os.getenv("MCP_OAUTH_PASSPHRASE", ""),
        pepper=os.getenv("MCP_OAUTH_SIGNING_SECRET", ""),
        store_path=os.getenv("MCP_OAUTH_STORE_PATH", "/data/oauth/oauth.db").strip(),
        access_ttl=_int_env("MCP_OAUTH_ACCESS_TTL", 3600),
        refresh_ttl=_int_env("MCP_OAUTH_REFRESH_TTL", 30 * 24 * 3600),
        code_ttl=_int_env("MCP_OAUTH_CODE_TTL", 600),
    )
```

- [ ] **Step 4: 运行（应通过）**

Run: `python -m unittest discover -s tests -p test_mcp_oauth_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/mcp-coding-server/app/oauth/__init__.py services/mcp-coding-server/app/oauth/config.py tests/test_mcp_oauth_config.py
git commit -m "feat(mcp-oauth): add OAuthConfig env loader"
```

## Task 3: `oauth/store.py`（SQLite 持久化）

**Files:**
- Create: `services/mcp-coding-server/app/oauth/store.py`
- Test: `tests/test_mcp_oauth_store.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_mcp_oauth_store.py
from __future__ import annotations
import time, unittest
# (粘贴 load_oauth() 加载器)

class StoreTests(unittest.TestCase):
    def _store(self):
        return load_oauth()["store"].OAuthStore(":memory:", "pepper-secret")

    def test_client_roundtrip(self):
        s = self._store()
        s.put_client("c1", '{"client_id":"c1"}')
        self.assertEqual(s.get_client("c1"), '{"client_id":"c1"}')
        self.assertIsNone(s.get_client("missing"))

    def test_pending_take_is_one_shot(self):
        s = self._store()
        s.put_pending("t1", "c1", '{"p":1}', ttl=60)
        self.assertEqual(s.take_pending("t1"), ("c1", '{"p":1}'))
        self.assertIsNone(s.take_pending("t1"))  # 已消费

    def test_pending_expired_returns_none(self):
        s = self._store()
        s.put_pending("t2", "c1", "{}", ttl=-1)
        self.assertIsNone(s.take_pending("t2"))

    def test_hashed_token_roundtrip_and_expiry(self):
        s = self._store()
        s.put_hashed("access_tokens", "rawtok", '{"client_id":"c1"}', ttl=60)
        self.assertEqual(s.get_hashed("access_tokens", "rawtok"), '{"client_id":"c1"}')
        self.assertIsNone(s.get_hashed("access_tokens", "wrong"))
        s.delete_hashed("access_tokens", "rawtok")
        self.assertIsNone(s.get_hashed("access_tokens", "rawtok"))

    def test_raw_value_not_stored_in_clear(self):
        s = self._store()
        s.put_hashed("access_tokens", "supersecret", "{}", ttl=60)
        # 原始 token 不应出现在 DB 主键中（只存哈希）
        rows = s._conn.execute("SELECT k FROM access_tokens").fetchall()
        self.assertNotIn("supersecret", [r[0] for r in rows])
```

- [ ] **Step 2: 运行（应失败）**

Run: `python -m unittest discover -s tests -p test_mcp_oauth_store.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现**

```python
# services/mcp-coding-server/app/oauth/store.py
"""SQLite-backed OAuth persistence.

opaque code/token 以 SHA-256(pepper + 原始值) 作主键存储，原始值不落盘；
data 列只存不含原始值的元数据 JSON（见 provider.py）。表名为内部常量、非用户输入。
"""
from __future__ import annotations
import hashlib
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_auth (txn TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS auth_codes (k TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS access_tokens (k TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS refresh_tokens (k TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
"""
_HASHED_TABLES = ("auth_codes", "access_tokens", "refresh_tokens")


class OAuthStore:
    def __init__(self, db_path: str, pepper: str) -> None:
        self._pepper = pepper.encode("utf-8")
        self._lock = threading.Lock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _hash(self, value: str) -> str:
        return hashlib.sha256(self._pepper + value.encode("utf-8")).hexdigest()

    # --- clients ---
    def put_client(self, client_id: str, data: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO clients(client_id,data) VALUES(?,?)", (client_id, data))
            self._conn.commit()

    def get_client(self, client_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM clients WHERE client_id=?", (client_id,)).fetchone()
        return row[0] if row else None

    # --- pending authorization (consent txn) ---
    def put_pending(self, txn: str, client_id: str, params: str, ttl: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_auth(txn,client_id,params,expires_at) VALUES(?,?,?,?)",
                (txn, client_id, params, time.time() + ttl),
            )
            self._conn.commit()

    def take_pending(self, txn: str) -> tuple[str, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT client_id,params,expires_at FROM pending_auth WHERE txn=?", (txn,)
            ).fetchone()
            self._conn.execute("DELETE FROM pending_auth WHERE txn=?", (txn,))
            self._conn.commit()
        if not row or row[2] < time.time():
            return None
        return row[0], row[1]

    # --- hashed-key tables (codes/tokens) ---
    def put_hashed(self, table: str, raw: str, data: str, ttl: int) -> None:
        assert table in _HASHED_TABLES
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {table}(k,data,expires_at) VALUES(?,?,?)",
                (self._hash(raw), data, time.time() + ttl),
            )
            self._conn.commit()

    def get_hashed(self, table: str, raw: str) -> str | None:
        assert table in _HASHED_TABLES
        with self._lock:
            row = self._conn.execute(
                f"SELECT data,expires_at FROM {table} WHERE k=?", (self._hash(raw),)
            ).fetchone()
        if not row or row[1] < time.time():
            return None
        return row[0]

    def delete_hashed(self, table: str, raw: str) -> None:
        assert table in _HASHED_TABLES
        with self._lock:
            self._conn.execute(f"DELETE FROM {table} WHERE k=?", (self._hash(raw),))
            self._conn.commit()
```

- [ ] **Step 4: 运行（应通过）**

Run: `python -m unittest discover -s tests -p test_mcp_oauth_store.py -v`
Expected: PASS（5 tests）

- [ ] **Step 5: Commit**

```bash
git add services/mcp-coding-server/app/oauth/store.py tests/test_mcp_oauth_store.py
git commit -m "feat(mcp-oauth): add SQLite OAuth store with peppered token hashing"
```

## Task 4: `oauth/provider.py`

**Files:**
- Create: `services/mcp-coding-server/app/oauth/provider.py`
- Test: `tests/test_mcp_oauth_provider.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_mcp_oauth_provider.py
from __future__ import annotations
import asyncio, unittest
# (粘贴 load_oauth() 加载器)
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull


def _run(coro): return asyncio.run(coro)


class ProviderTests(unittest.TestCase):
    def _provider(self):
        m = load_oauth()
        cfg = m["config"].OAuthConfig(enabled=True, issuer_url="https://h.xyz/mcp-x",
            passphrase="pw", pepper="p"*32, store_path=":memory:", access_ttl=60, refresh_ttl=600, code_ttl=60)
        store = m["store"].OAuthStore(":memory:", cfg.pepper)
        return m["provider"].AliecsOAuthProvider(cfg, store), cfg

    def _client(self):
        return OAuthClientInformationFull(client_id="c1", redirect_uris=["https://chatgpt.com/cb"])

    def _params(self):
        return AuthorizationParams(state="st", scopes=["coding"], code_challenge="chal",
            redirect_uri="https://chatgpt.com/cb", redirect_uri_provided_explicitly=True, resource=None)

    def test_register_then_get_client(self):
        p, _ = self._provider(); c = self._client()
        _run(p.register_client(c))
        got = _run(p.get_client("c1"))
        self.assertIsNotNone(got); self.assertEqual(got.client_id, "c1")

    def test_authorize_returns_consent_url_and_stores_pending(self):
        p, cfg = self._provider(); c = self._client()
        _run(p.register_client(c))
        url = _run(p.authorize(c, self._params()))
        self.assertTrue(url.startswith(cfg.issuer_url + "/oauth/consent?txn="))

    def test_complete_then_exchange_code_issues_tokens(self):
        p, _ = self._provider(); c = self._client()
        _run(p.register_client(c)); params = self._params()
        # 模拟同意通过：直接 complete_authorization
        redirect = p.complete_authorization("c1", params)
        self.assertIn("code=", redirect)
        code = redirect.split("code=")[1].split("&")[0]
        auth_code = _run(p.load_authorization_code(c, code))
        self.assertIsNotNone(auth_code)
        self.assertEqual(auth_code.code_challenge, "chal")
        tok = _run(p.exchange_authorization_code(c, auth_code))
        self.assertTrue(tok.access_token and tok.refresh_token)
        # access token 可被 load
        at = _run(p.load_access_token(tok.access_token))
        self.assertIsNotNone(at); self.assertEqual(at.client_id, "c1")
        # code 一次性
        self.assertIsNone(_run(p.load_authorization_code(c, code)))

    def test_refresh_rotates(self):
        p, _ = self._provider(); c = self._client()
        _run(p.register_client(c))
        redirect = p.complete_authorization("c1", self._params())
        code = redirect.split("code=")[1].split("&")[0]
        tok = _run(p.exchange_authorization_code(c, _run(p.load_authorization_code(c, code))))
        rt = _run(p.load_refresh_token(c, tok.refresh_token))
        tok2 = _run(p.exchange_refresh_token(c, rt, ["coding"]))
        self.assertTrue(tok2.access_token)
        self.assertIsNone(_run(p.load_refresh_token(c, tok.refresh_token)))  # 旧 refresh 失效

    def test_revoke(self):
        p, _ = self._provider(); c = self._client()
        _run(p.register_client(c))
        redirect = p.complete_authorization("c1", self._params())
        code = redirect.split("code=")[1].split("&")[0]
        tok = _run(p.exchange_authorization_code(c, _run(p.load_authorization_code(c, code))))
        at = _run(p.load_access_token(tok.access_token))
        _run(p.revoke_token(at))
        self.assertIsNone(_run(p.load_access_token(tok.access_token)))
```

- [ ] **Step 2: 运行（应失败）**

Run: `python -m unittest discover -s tests -p test_mcp_oauth_provider.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现**

```python
# services/mcp-coding-server/app/oauth/provider.py
from __future__ import annotations
import json
import secrets
import time

from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationParams,
    AuthorizationCode,
    AccessToken,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import OAuthConfig
from .store import OAuthStore


class AliecsOAuthProvider(OAuthAuthorizationServerProvider):
    """自托管极简 AS：DCR + 单口令同意 + opaque token（SQLite，pepper 哈希）。
    PKCE 由 SDK TokenHandler 校验，本类只存 code_challenge。"""

    def __init__(self, config: OAuthConfig, store: OAuthStore) -> None:
        self.config = config
        self.store = store

    # --- clients (DCR) ---
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put_client(client_info.client_id, client_info.model_dump_json())

    # --- authorize：存 txn，跳同意页 ---
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        txn = secrets.token_urlsafe(24)
        self.store.put_pending(txn, client.client_id, params.model_dump_json(), self.config.txn_ttl)
        return f"{self.config.issuer_url}/oauth/consent?txn={txn}"

    def complete_authorization(self, client_id: str, params: AuthorizationParams) -> str:
        """口令通过后由 consent 路由调用：签发授权码，返回带 code 的 client 回跳 URL。"""
        code = secrets.token_urlsafe(32)
        meta = {
            "scopes": list(params.scopes or [self.config.scope]),
            "client_id": client_id,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": str(params.resource) if params.resource else None,
        }
        self.store.put_hashed("auth_codes", code, json.dumps(meta), self.config.code_ttl)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        data = self.store.get_hashed("auth_codes", authorization_code)
        if not data:
            return None
        m = json.loads(data)
        return AuthorizationCode(
            code=authorization_code,
            scopes=m["scopes"],
            expires_at=int(time.time() + self.config.code_ttl),
            client_id=m["client_id"],
            code_challenge=m["code_challenge"],
            redirect_uri=m["redirect_uri"],
            redirect_uri_provided_explicitly=m["redirect_uri_provided_explicitly"],
            resource=m.get("resource"),
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        self.store.delete_hashed("auth_codes", authorization_code.code)
        return self._issue(authorization_code.client_id, list(authorization_code.scopes), authorization_code.resource)

    # --- refresh ---
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        data = self.store.get_hashed("refresh_tokens", refresh_token)
        if not data:
            return None
        m = json.loads(data)
        return RefreshToken(token=refresh_token, client_id=m["client_id"], scopes=m["scopes"], expires_at=m.get("expires_at"))

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        self.store.delete_hashed("refresh_tokens", refresh_token.token)
        return self._issue(refresh_token.client_id, list(scopes or refresh_token.scopes), None)

    # --- access ---
    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.store.get_hashed("access_tokens", token)
        if not data:
            return None
        m = json.loads(data)
        return AccessToken(token=token, client_id=m["client_id"], scopes=m["scopes"],
                           expires_at=m.get("expires_at"), resource=m.get("resource"))

    async def revoke_token(self, token) -> None:
        raw = getattr(token, "token", None)
        if raw:
            self.store.delete_hashed("access_tokens", raw)
            self.store.delete_hashed("refresh_tokens", raw)

    # --- helpers ---
    def _issue(self, client_id: str, scopes: list[str], resource) -> OAuthToken:
        now = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        res = str(resource) if resource else None
        self.store.put_hashed("access_tokens", access, json.dumps(
            {"client_id": client_id, "scopes": scopes, "expires_at": int(now + self.config.access_ttl), "resource": res}
        ), self.config.access_ttl)
        self.store.put_hashed("refresh_tokens", refresh, json.dumps(
            {"client_id": client_id, "scopes": scopes, "expires_at": int(now + self.config.refresh_ttl)}
        ), self.config.refresh_ttl)
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=self.config.access_ttl, scope=" ".join(scopes), refresh_token=refresh)
```

- [ ] **Step 4: 运行（应通过）**

Run: `python -m unittest discover -s tests -p test_mcp_oauth_provider.py -v`
Expected: PASS（6 tests）。
> 若出现 pydantic 校验错误（如 `expires_at`/`resource` 类型不符），按报错调整对应字段的构造（如改用 `int` 时间戳、或传 `AnyUrl`）—— 这是预期内的小迭代，不是放置占位。**禁止**为过测试削弱任何校验逻辑。

- [ ] **Step 5: Commit**

```bash
git add services/mcp-coding-server/app/oauth/provider.py tests/test_mcp_oauth_provider.py
git commit -m "feat(mcp-oauth): add AliecsOAuthProvider (DCR + code/token lifecycle)"
```

## Task 5: `oauth/consent.py`（单口令同意页）

**Files:**
- Create: `services/mcp-coding-server/app/oauth/consent.py`
- Test: `tests/test_mcp_oauth_consent.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_mcp_oauth_consent.py
from __future__ import annotations
import asyncio, unittest
from unittest import mock
# (粘贴 load_oauth() 加载器)
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request


def _run(c): return asyncio.run(c)

def _get_request(path, query=b""):
    scope = {"type": "http", "method": "GET", "path": path, "query_string": query, "headers": []}
    return Request(scope)

def _post_request(form: dict):
    body = "&".join(f"{k}={v}" for k, v in form.items()).encode()
    sent = {"done": False}
    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}
    scope = {"type": "http", "method": "POST", "path": "/oauth/consent", "query_string": b"",
             "headers": [(b"content-type", b"application/x-www-form-urlencoded")]}
    return Request(scope, receive)


class ConsentTests(unittest.TestCase):
    def _setup(self):
        m = load_oauth()
        cfg = m["config"].OAuthConfig(enabled=True, issuer_url="https://h.xyz/mcp-x",
            passphrase="hunter2", pepper="p"*32, store_path=":memory:", code_ttl=60)
        store = m["store"].OAuthStore(":memory:", cfg.pepper)
        prov = m["provider"].AliecsOAuthProvider(cfg, store)
        handler = m["consent"].make_consent_handler(prov, cfg)
        # 预置一个 pending txn
        params = AuthorizationParams(state="st", scopes=["coding"], code_challenge="chal",
            redirect_uri="https://chatgpt.com/cb", redirect_uri_provided_explicitly=True, resource=None)
        store.put_pending("TXN", "c1", params.model_dump_json(), ttl=60)
        return handler

    def test_get_renders_form(self):
        h = self._setup()
        resp = _run(h(_get_request("/oauth/consent", b"txn=TXN")))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"txn", resp.body); self.assertIn(b"password", resp.body)

    def test_post_wrong_passphrase_rejected(self):
        h = self._setup()
        resp = _run(h(_post_request({"txn": "TXN", "passphrase": "wrong"})))
        self.assertEqual(resp.status_code, 403)

    def test_post_correct_passphrase_redirects_with_code(self):
        h = self._setup()
        resp = _run(h(_post_request({"txn": "TXN", "passphrase": "hunter2"})))
        self.assertIn(resp.status_code, (302, 303, 307))
        self.assertIn("code=", resp.headers["location"])
        self.assertIn("state=st", resp.headers["location"])
```

- [ ] **Step 2: 运行（应失败）**

Run: `python -m unittest discover -s tests -p test_mcp_oauth_consent.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现**

```python
# services/mcp-coding-server/app/oauth/consent.py
from __future__ import annotations
import hmac
import json

from mcp.server.auth.provider import AuthorizationParams
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .config import OAuthConfig
from .provider import AliecsOAuthProvider

_FORM = """<!doctype html><html><head><meta charset="utf-8"><title>AliECS Coding 授权</title></head>
<body style="font-family:sans-serif;max-width:420px;margin:64px auto">
<h3>AliECS Coding 连接授权</h3>
<p>请输入授权口令以允许 ChatGPT 连接。</p>
{error}
<form method="post" action="/oauth/consent">
<input type="hidden" name="txn" value="{txn}">
<input type="password" name="passphrase" autofocus style="width:100%;padding:8px;font-size:16px" placeholder="口令">
<button type="submit" style="margin-top:12px;padding:8px 16px">授权</button>
</form></body></html>"""


def make_consent_handler(provider: AliecsOAuthProvider, config: OAuthConfig):
    async def consent(request: Request):
        if request.method == "GET":
            txn = request.query_params.get("txn", "")
            return HTMLResponse(_FORM.format(txn=_esc(txn), error=""))
        # POST
        form = await request.form()
        txn = str(form.get("txn", ""))
        passphrase = str(form.get("passphrase", ""))
        if not hmac.compare_digest(passphrase, config.passphrase):
            return HTMLResponse(
                _FORM.format(txn=_esc(txn), error='<p style="color:#c00">口令错误</p>'),
                status_code=403,
            )
        pending = provider.store.take_pending(txn)
        if pending is None:
            return HTMLResponse("<p>授权请求已过期，请在 ChatGPT 重新连接。</p>", status_code=400)
        client_id, params_json = pending
        params = AuthorizationParams.model_validate_json(params_json)
        redirect = provider.complete_authorization(client_id, params)
        return RedirectResponse(redirect, status_code=302)

    return consent


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
```

- [ ] **Step 4: 运行（应通过）**

Run: `python -m unittest discover -s tests -p test_mcp_oauth_consent.py -v`
Expected: PASS（3 tests）。
> 若 `await request.form()` 在测试构造的 Request 上报错，按报错把 `_post_request` 的 receive 协议补全（body 帧）—— 已按 Starlette 协议给出，正常即可。

- [ ] **Step 5: Commit**

```bash
git add services/mcp-coding-server/app/oauth/consent.py tests/test_mcp_oauth_consent.py
git commit -m "feat(mcp-oauth): add single-passphrase consent route"
```

## Task 6: 装配进 `main.py` + 集成测试

**Files:**
- Modify: `services/mcp-coding-server/app/main.py`
- Test: `tests/test_mcp_oauth_integration.py`

- [ ] **Step 1: 改 `main.py`**

在 `from . import executor_client` 后加：

```python
from .oauth.config import config_from_env as _oauth_config_from_env
from .oauth.store import OAuthStore as _OAuthStore
from .oauth.provider import AliecsOAuthProvider as _AliecsOAuthProvider
from .oauth.consent import make_consent_handler as _make_consent_handler
```

把现有 `mcp = FastMCP( ... )` 调用替换为按开关装配（保留原有所有参数，仅在启用时追加 auth 相关 kwargs 与 consent 路由）：

```python
_OAUTH_CONFIG = _oauth_config_from_env()
_oauth_kwargs: dict = {}
_oauth_provider = None
if _OAUTH_CONFIG.enabled:
    if not _OAUTH_CONFIG.fully_configured:
        raise RuntimeError(
            "MCP_OAUTH_ENABLED=true 但缺少 MCP_OAUTH_ISSUER/PASSPHRASE/SIGNING_SECRET/STORE_PATH"
        )
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    _oauth_store = _OAuthStore(_OAUTH_CONFIG.store_path, _OAUTH_CONFIG.pepper)
    _oauth_provider = _AliecsOAuthProvider(_OAUTH_CONFIG, _oauth_store)
    _oauth_kwargs = dict(
        auth_server_provider=_oauth_provider,
        auth=AuthSettings(
            issuer_url=_OAUTH_CONFIG.issuer_url,
            resource_server_url=_OAUTH_CONFIG.issuer_url,
            required_scopes=[_OAUTH_CONFIG.scope],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[_OAUTH_CONFIG.scope], default_scopes=[_OAUTH_CONFIG.scope]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

mcp = FastMCP(
    SERVER_NAME,
    instructions=( ...原文不变... ),
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8090")),
    stateless_http=True,
    json_response=True,
    **_oauth_kwargs,
)

if _oauth_provider is not None:
    mcp.custom_route("/oauth/consent", methods=["GET", "POST"])(
        _make_consent_handler(_oauth_provider, _OAUTH_CONFIG)
    )
```

并把 `PHASE` 改为 `"phase-4-oauth"`、`SERVER_VERSION` 提到 `"0.3.0"`，`server_info_payload()` 的 `note` 补一句「阶段四：连接器需 OAuth 鉴权」。

> `AuthSettings` 字段名以 Task 关键事实为准（`issuer_url/resource_server_url/required_scopes/client_registration_options/revocation_options`）。若构造报字段错误，用 `python -c "from mcp.server.auth.settings import AuthSettings; print(AuthSettings.model_fields)"` 核对后调整。

- [ ] **Step 2: 写集成测试**

```python
# tests/test_mcp_oauth_integration.py
from __future__ import annotations
import importlib, os, sys, unittest
from unittest import mock
from pathlib import Path
from fastapi.testclient import TestClient  # starlette TestClient 亦可

SVC = Path(__file__).resolve().parents[1] / "services" / "mcp-coding-server" / "app"

def _load_main(env):
    for n in list(sys.modules):
        if n.split(".")[0] in ("app",) or n.startswith("mcp_oauth_pkg"):
            sys.modules.pop(n, None)
    import importlib.util
    spec = importlib.util.spec_from_file_location("app", SVC / "__init__.py", submodule_search_locations=[str(SVC)])
    pkg = importlib.util.module_from_spec(spec); sys.modules["app"] = pkg; spec.loader.exec_module(pkg)
    # 子模块按需由相对导入自动加载；直接加载 main
    s = importlib.util.spec_from_file_location("app.main", SVC / "main.py")
    main = importlib.util.module_from_spec(s); sys.modules["app.main"] = main
    with mock.patch.dict(os.environ, env, clear=False):
        s.loader.exec_module(main)
    return main


class IntegrationTests(unittest.TestCase):
    def test_disabled_keeps_healthz_open_no_auth(self):
        main = _load_main({"MCP_OAUTH_ENABLED": "false"})
        app = main.mcp.streamable_http_app()
        client = TestClient(app)
        self.assertEqual(client.get("/healthz").status_code, 200)

    def test_enabled_serves_metadata_and_consent_and_keeps_healthz_open(self):
        import tempfile
        db = os.path.join(tempfile.gettempdir(), "oauth-int.db")
        if os.path.exists(db): os.remove(db)
        env = {"MCP_OAUTH_ENABLED": "true", "MCP_OAUTH_ISSUER": "https://h.xyz/mcp-x",
               "MCP_OAUTH_PASSPHRASE": "pw", "MCP_OAUTH_SIGNING_SECRET": "p"*32,
               "MCP_OAUTH_STORE_PATH": db}
        main = _load_main(env)
        app = main.mcp.streamable_http_app()
        client = TestClient(app)
        # healthz 仍公开
        self.assertEqual(client.get("/healthz").status_code, 200)
        # AS 元数据可发现（免鉴权）
        r = client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(r.status_code, 200)
        self.assertIn("authorization_endpoint", r.json())
        # protected-resource 元数据
        self.assertEqual(client.get("/.well-known/oauth-protected-resource").status_code, 200)
```

- [ ] **Step 3: 运行集成测试**

Run: `python -m unittest discover -s tests -p test_mcp_oauth_integration.py -v`
Expected: PASS。
> 关键验证点：①`MCP_OAUTH_ENABLED=false` 时 `/healthz` 200 且工具不需 token（保持现状）；②启用时 `/.well-known/oauth-authorization-server` 与 `/.well-known/oauth-protected-resource` 返回 200、`/healthz` 仍 200（自定义路由不被鉴权中间件包裹）。
> **HARD STOP**：若启用后 `/healthz` 变成 401/403（自定义路由被鉴权拦），停下报告——需改为把 healthz/consent 挂在鉴权中间件之外的方式，属设计偏差需复核，**不要**靠关掉鉴权绕过。
> `streamable_http_app()` 若不是正确的 app 获取方法，用 `python -c "from mcp.server.fastmcp import FastMCP; print([a for a in dir(FastMCP) if 'app' in a.lower()])"` 找到正确方法名后替换。

- [ ] **Step 4: Commit**

```bash
git add services/mcp-coding-server/app/main.py tests/test_mcp_oauth_integration.py
git commit -m "feat(mcp-oauth): wire OAuth AS/RS into FastMCP behind MCP_OAUTH_ENABLED"
```

## Task 7: 文档与部署/人工步骤

**Files:**
- Modify: `services/mcp-coding-server/README.md`（无则创建）

- [ ] **Step 1: 写 README 的「OAuth（阶段四）」小节**

包含：env 变量表（`MCP_OAUTH_ENABLED/ISSUER/PASSPHRASE/SIGNING_SECRET/STORE_PATH/*_TTL`，真实值只在 ECS runtime.env、不进 git）；持久化卷（`MCP_OAUTH_STORE_PATH` 需挂卷，建议 compose 给 mcp-coding-server 加一个 named volume 挂到 `/data/oauth`）；Nginx 需在秘密路径下代理 `/.well-known/oauth-*`、`/authorize`、`/token`、`/register`、`/revoke`、`/oauth/consent` 及现有 MCP 与 `/healthz`；issuer 必须等于秘密路径公网完整 URL。

- [ ] **Step 2: 写「人工上线步骤」清单（运维红线）**

```
1. ECS runtime.env 填入：MCP_OAUTH_PASSPHRASE / MCP_OAUTH_SIGNING_SECRET（你已生成）
   / MCP_OAUTH_ISSUER（=秘密路径 URL）/ MCP_OAUTH_STORE_PATH=/data/oauth/oauth.db；先保持 MCP_OAUTH_ENABLED=false 部署一次（确认无回归）。
2. compose.prod.yml 给 mcp-coding-server 挂 /data/oauth 卷；Nginx 增补上述路由代理；reload。
3. 置 MCP_OAUTH_ENABLED=true 部署；浏览器访问 issuer + /.well-known/oauth-authorization-server 应见 JSON。
4. ChatGPT 连接器：当前 auth=无，可能需「取消关联」后用同一 URL 重新添加，使其走 OAuth；浏览器弹同意页时输入口令。
5. 验证 server_info / ping 仍可用（已带 token）。再考虑接入写工具。
```

- [ ] **Step 3: Commit**

```bash
git add services/mcp-coding-server/README.md
git commit -m "docs(mcp-oauth): document Phase 4 env, volume, nginx and manual rollout"
```

## Task 8: 全量回归 + 收尾

- [ ] **Step 1: 跑全量（复现共享 venv）**

Run: `python -m unittest discover -s tests`
Expected: OK，无回归（含既有 `test_mcp_coding_server` 与所有新 oauth 测试）。

- [ ] **Step 2: 语法/导入冒烟**

Run: `python -c "import sys; sys.path.insert(0,'services/mcp-coding-server'); import os; os.environ['MCP_OAUTH_ENABLED']='false'; from app import main; print(main.PHASE)"`
Expected: `phase-4-oauth`

- [ ] **Step 3:（如未在各 Task 提交）补一次收尾 commit**

```bash
git add -A
git commit -m "test(mcp-oauth): full suite green for Phase 4" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:**
- 自托管 AS+RS（SDK provider） → Task 4 + Task 6。
- 元数据发现 / DCR / authorize / token / revoke → Task 6 装配（SDK `create_auth_routes` 自动挂）+ Task 4 provider。
- 单口令同意页 → Task 5。
- 持久化（client/code/token + 签名密钥即 pepper） → Task 3 + Task 4。
- RS 校验（工具需 token、healthz/well-known 公开） → Task 6 集成测试。
- 纵深防御（秘密路径保留）/ Nginx / 人工步骤 → Task 7。
- 灰度开关 `MCP_OAUTH_ENABLED` → Task 2 + Task 6。
- 版本/依赖（mcp 1.27 与 starlette<0.48 兼容，无需拆 CI） → Task 1 + 关键事实。

**Placeholder scan:** 无 TODO/TBD；每个写代码步骤都给了完整代码；少数 SDK 集成不确定点给的是「明确的核对命令 + 按报错调整」而非占位（参照本仓 Phase 3a 计划对 `.fn` 的处理），且明确禁止削弱校验。

**Type/signature consistency:** `OAuthConfig`(Task2) 字段被 store/provider/consent/main 一致引用；`OAuthStore` 方法 `put/get/delete_hashed`、`put/take_pending`、`put/get_client`(Task3) 与 provider(Task4) 调用一致；`AliecsOAuthProvider` 9 方法签名与 SDK 契约一致；`make_consent_handler(provider, config)`(Task5) 与 main(Task6) 调用一致。

---

## Operator Prompt Template (Codex, 无人值守)

```text
You are executing an approved implementation plan end-to-end without stopping
for confirmation, except at the Hard Stop Conditions below.

Plan file: docs/superpowers/plans/2026-06-13-mcp-oauth-phase4.md
Repo root: this checkout of AliECS (base = origin/main 含 #105/#106)

IMPORTANT CONTEXT: 本计划给一个「公网可达、且具备写+commit 能力」的 MCP 服务加
OAuth 鉴权。安全是第一位：绝不为了过测试而削弱口令校验(hmac.compare_digest)、
PKCE(由 SDK 校验，本代码只存 code_challenge)、token 哈希(pepper)或作用域校验。

Rules:
1. 按 Task 1 → Task 8 顺序执行。每个 Task 内严格 TDD：先写失败测试→运行确认失败
   →最小实现→运行确认通过→commit。一个「Commit」步骤一个 commit，别批量。
2. 每个 Task 收尾后运行：python -m unittest discover -s tests
   失败必须先修好再进下一个 Task。
3. 只改本计划「Files」列出的文件。不新增运行时依赖（除 Task 1 的 mcp 版本上调）。
4. 测试一律用 importlib 文件加载法（见计划 File Structure 的 load_oauth()），
   规避 backend-api 的 `app` 包名冲突；本地必须能全量 discover 复现。
5. 遇到 SDK 字段/方法名不符：用计划中给出的核对命令
   (AuthSettings.model_fields / FastMCP app 方法 / 等) 核实后调整，这是预期内调查。

Hard Stop Conditions（停下并报告，不要硬闯）：
- 启用 OAuth 后 /healthz 或 /.well-known/* 变成需要鉴权（401/403）——属设计偏差。
- 任何步骤要求 git push / 部署 / SSH / 真实密钥 / 生产主机名——本计划只做本地
  commit，部署与连接器重配是人工步骤(Task 7 文档)，禁止自动执行。
- 你发现自己想削弱 hmac.compare_digest、PKCE、token 哈希或 required_scopes 来过测试。
- 某测试的失败信息与计划描述不符，且重读被引用源码/用核对命令仍无法解释。

全部 8 个 Task 提交、且 python -m unittest discover -s tests 通过后，报告：
- 完成了哪些 Task；
- git log --oneline -n 15；
- python -m unittest discover -s tests 末 20 行；
- 提醒：MCP_OAUTH_ENABLED 默认 false；上线需人工按 Task 7 配置 ECS env/卷/Nginx
  并重配 ChatGPT 连接器（auth 无→OAuth）。接入写工具前先验证 OAuth 握手。
```
