# 飞书手动同步 + 整簿重扫 实施计划（PR①）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `/exports/` 的手动同步对飞书多维表格生效（现在一律 failed「暂不支持该 provider：feishu」），并让飞书定时/手动同步都整簿重扫——新建的数据表自动收录、已删的标记 disabled，与企微行为对齐。

**Architecture:** 全部改动在 `services/doc-sync-worker`。新增 `_rescan_app_tables`（list_tables → ensure_source → disable_missing_sheets）和 `sync_feishu_source`（消费单个 source_id 的手动请求，doc 级=整簿重扫+全表同步，table 级=单表同步）；`run_sync_feishu_full` 改为"seed 源解析 app_token → 每个 app 整簿重扫 → 逐表同步（单表失败不拖垮整轮）"；`run_pending_sync_requests` 按 request.provider 分发 wecom/feishu。

**Tech Stack:** Python 3.11 / psycopg / unittest（根 `tests/test_doc_sync_worker.py`，CI 在 PR 上跑）。

## Global Constraints

- 仓库：`AliECS`（走 PR 合并 main；main merge 触发 release-deploy）。分支名 `feature/feishu-manual-sync-rescan`，从 `origin/main` 新建。
- 测试命令（在 AliECS 根目录）：`python -m pytest tests/test_doc_sync_worker.py -v`（测试文件自带 sys.path 注入，无需 PYTHONPATH）。
- 提交前跑全量根测试 `python -m pytest tests/ -x -q`。
- 不改 bridge、不改 backend-api、不改 deploy 配置；本 PR 只动 `services/doc-sync-worker` 与测试。
- git add 必须显式列文件路径，禁止 `-A`/`.`/`-u`。
- 已有 store 方法直接复用：`ensure_source(**) -> int`、`disable_missing_sheets(provider, env_profile, external_doc_id, seen_sheet_ids) -> int`（seen 为空时不动、返回 0）、`list_registry_doc_sources(provider, env_profile) -> list[dict]`（doc 级登记行，含 `/exports/` 页"同步数据列表"建的 `smartsheet_doc` 行）、`get_source(source_id) -> dict|None`、`start_run/finish_run`。

---

### Task 1: `_rescan_app_tables` 整簿重扫助手

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`（在 `_resolve_app_token` 之后新增函数）
- Test: `tests/test_doc_sync_worker.py`（`FeishuBitableSyncTests` 类内追加）

**Interfaces:**
- Consumes: `client.list_tables(app_token) -> list[dict]`、`store.ensure_source(**) -> int`、`store.disable_missing_sheets(...) -> int`、`compose_source_name(doc, sheet)`、`FeishuBitableSource`
- Produces: `_rescan_app_tables(store, client, profile, app_token, document_name, source_url="", view_ids=None) -> tuple[list[tuple[int, FeishuBitableSource]], int]` —— 返回（[(source_id, source), ...] 按 list_tables 顺序, 停用数）。Task 2/3 依赖此签名。

- [ ] **Step 1: 写失败测试**

```python
    def test_rescan_app_tables_registers_new_tables_and_disables_missing(self) -> None:
        from app.pipelines.sync_feishu_full import _rescan_app_tables

        class FakeClient:
            def list_tables(self, app_token: str) -> list[dict]:
                return [
                    {"table_id": "tbl_sessions", "name": "会话索引表"},
                    {"table_id": "tbl_notes", "name": "使用说明"},
                ]

        class FakeStore:
            def __init__(self) -> None:
                self.sources: list[dict] = []
                self.disabled_args: tuple | None = None

            def ensure_source(self, **kwargs: object) -> int:
                self.sources.append(dict(kwargs))
                return len(self.sources)

            def disable_missing_sheets(
                self, provider: str, env_profile: str, external_doc_id: str, seen_sheet_ids: list[str]
            ) -> int:
                self.disabled_args = (provider, env_profile, external_doc_id, list(seen_sheet_ids))
                return 1

        store = FakeStore()
        pairs, disabled = _rescan_app_tables(
            store,
            FakeClient(),  # type: ignore[arg-type]
            "COMPANY_A",
            "bascn_console",
            "飞书 ChatGPT 会话管理台",
            source_url="https://feishu.cn/base/bascn_console",
            view_ids={"tbl_sessions": "view_1"},
        )

        self.assertEqual(2, len(pairs))
        self.assertEqual(1, disabled)
        source_id, source = pairs[1]
        self.assertEqual(2, source_id)
        self.assertEqual("tbl_notes", source.table_id)
        self.assertEqual("使用说明", source.sheet_name)
        self.assertEqual("view_1", pairs[0][1].view_id)
        self.assertEqual("", pairs[1][1].view_id)
        self.assertEqual("bitable_table", store.sources[0]["source_type"])
        self.assertEqual("飞书 ChatGPT 会话管理台 / 使用说明", store.sources[1]["source_name"])
        self.assertEqual(
            ("feishu", "COMPANY_A", "bascn_console", ["tbl_sessions", "tbl_notes"]),
            store.disabled_args,
        )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_doc_sync_worker.py -k rescan_app_tables -v`
Expected: FAIL `ImportError: cannot import name '_rescan_app_tables'`

- [ ] **Step 3: 实现**

在 `sync_feishu_full.py` 的 `_resolve_app_token` 之后加：

```python
def _rescan_app_tables(
    store: Any,
    client: FeishuBitableClient,
    profile: str,
    app_token: str,
    document_name: str,
    source_url: str = "",
    view_ids: dict[str, str] | None = None,
) -> tuple[list[tuple[int, FeishuBitableSource]], int]:
    """整簿重扫：列出工作簿全部数据表并登记为同步源，返回 ([(source_id, source), ...], 停用数)。
    新表自动收录；本轮没看到的表标记 disabled（list_tables 返回空时不剪，防误伤）。"""
    doc_name = str(document_name or app_token)
    views = view_ids or {}
    pairs: list[tuple[int, FeishuBitableSource]] = []
    seen_table_ids: list[str] = []
    for item in client.list_tables(app_token):
        table_id = _table_id(item)
        if not table_id:
            continue
        sheet_name = _table_name(item) or table_id
        seen_table_ids.append(table_id)
        source = FeishuBitableSource(
            env_profile=profile,
            app_token=app_token,
            table_id=table_id,
            source_name=sheet_name,
            view_id=views.get(table_id, ""),
            source_url=source_url,
            document_name=doc_name,
            sheet_name=sheet_name,
        )
        source_id = store.ensure_source(
            provider="feishu",
            env_profile=profile,
            source_name=compose_source_name(doc_name, sheet_name),
            source_type="bitable_table",
            external_doc_id=app_token,
            external_sheet_id=table_id,
            source_url=source_url,
            document_name=doc_name,
            sheet_name=sheet_name,
        )
        pairs.append((source_id, source))
    disabled = store.disable_missing_sheets("feishu", profile, app_token, seen_table_ids)
    return pairs, disabled
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_doc_sync_worker.py -k rescan_app_tables -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add services/doc-sync-worker/app/pipelines/sync_feishu_full.py tests/test_doc_sync_worker.py
git commit -m "feat(doc-sync): 飞书整簿重扫助手 _rescan_app_tables（新表收录/删表停用）"
```

---

### Task 2: `sync_feishu_source` 手动请求处理

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`（文件末尾、`run_sync_feishu_full` 之前新增）
- Test: `tests/test_doc_sync_worker.py`（新增 `FeishuManualSyncTests` 类）

**Interfaces:**
- Consumes: Task 1 的 `_rescan_app_tables`；既有 `_sync_bitable_records(store, client, source_id, app_token, table_id, view_id, counts, source_name)`、`credentials_for_profile(profile)`、`FeishuBitableClient`
- Produces: `sync_feishu_source(store, source_id, mode="manual") -> tuple[str, int | None, dict[str, Any]]` —— 与 `sync_wecom_source` 同形（status, run_id, detail）。Task 3 的分发依赖它。

- [ ] **Step 1: 写失败测试**

```python
class FeishuManualSyncTests(WorkerImportTestCase):
    class _Store:
        """sync_feishu_source 所需的最小 FakeStore。"""

        def __init__(self, source: dict) -> None:
            self.source = source
            self.runs: list[dict] = []
            self.finished: dict | None = None
            self.sources: list[dict] = []

        def get_source(self, source_id: int) -> dict | None:
            return dict(self.source) if source_id == self.source["id"] else None

        def start_run(self, provider: str, env_profile: str, mode: str) -> int:
            self.runs.append({"provider": provider, "env_profile": env_profile, "mode": mode})
            return 42

        def finish_run(self, run_id: int, status: str, counts: dict, error_json: list) -> None:
            self.finished = {"run_id": run_id, "status": status, "counts": dict(counts), "errors": list(error_json)}

        def ensure_source(self, **kwargs: object) -> int:
            self.sources.append(dict(kwargs))
            return len(self.sources)

        def disable_missing_sheets(self, provider: str, env_profile: str, external_doc_id: str, seen: list) -> int:
            return 0

        def replace_fields(self, source_id: int, fields: list) -> dict:
            return {}

        def upsert_record(self, source_id: int, snapshot: object) -> object:
            from app.storage.postgres import UpsertDecision

            return UpsertDecision(action="create", should_write=True)

        def mark_source_synced(self, source_id: int) -> None:
            return None

    class _Client:
        def list_tables(self, app_token: str) -> list[dict]:
            return [{"table_id": "tbl_a", "name": "会话索引表"}, {"table_id": "tbl_b", "name": "使用说明"}]

        def list_fields(self, app_token: str, table_id: str) -> list[dict]:
            return []

        def get_records(self, app_token: str, table_id: str, view_id: str = "") -> dict:
            return {"records": [{"record_id": f"rec_{table_id}", "fields": {}}], "page_count": 1}

    def _run(self, source: dict) -> tuple:
        import unittest.mock as mock

        from app.pipelines import sync_feishu_full as module

        # credentials 只取 [0] 的 app_id/app_secret/api_base，用简单对象即可
        class Cred:
            app_id = "cli_x"
            app_secret = "s"
            api_base = "https://open.feishu.cn/open-apis"

        store = self._Store(source)
        with mock.patch.object(module, "credentials_for_profile", return_value=[Cred()]), mock.patch.object(
            module, "FeishuBitableClient", return_value=self._Client()
        ):
            result = module.sync_feishu_source(store, source_id=source["id"], mode="manual")
        return store, result

    def test_doc_level_request_rescans_and_syncs_all_tables(self) -> None:
        store, (status, run_id, detail) = self._run(
            {
                "id": 1619,
                "provider": "feishu",
                "env_profile": "COMPANY_A",
                "source_name": "飞书 ChatGPT 会话管理台",
                "source_type": "smartsheet_doc",
                "external_doc_id": "bascn_console",
                "external_sheet_id": "",
                "source_url": "",
                "status": "active",
                "sheet_name": "",
            }
        )
        self.assertEqual("success", status)
        self.assertEqual(42, run_id)
        self.assertEqual(2, len(store.sources))
        self.assertEqual("manual", store.runs[0]["mode"])
        self.assertEqual(2, store.finished["counts"]["sheet_count"])
        self.assertEqual(2, store.finished["counts"]["record_count"])

    def test_table_level_request_syncs_single_table(self) -> None:
        store, (status, run_id, detail) = self._run(
            {
                "id": 1218,
                "provider": "feishu",
                "env_profile": "COMPANY_A",
                "source_name": "飞书 ChatGPT 会话管理台 / 消息日志表",
                "source_type": "bitable_table",
                "external_doc_id": "bascn_console",
                "external_sheet_id": "tbl_messages",
                "source_url": "",
                "status": "active",
                "sheet_name": "消息日志表",
            }
        )
        self.assertEqual("success", status)
        self.assertEqual(0, len(store.sources))  # 单表请求不重扫
        self.assertEqual(1, store.finished["counts"]["sheet_count"])

    def test_non_feishu_source_fails_without_run(self) -> None:
        from app.pipelines.sync_feishu_full import sync_feishu_source

        store = self._Store(
            {
                "id": 9,
                "provider": "wecom",
                "env_profile": "COMPANY_A",
                "source_name": "x",
                "source_type": "smartsheet_doc",
                "external_doc_id": "dc",
                "external_sheet_id": "",
                "source_url": "",
                "status": "active",
                "sheet_name": "",
            }
        )
        status, run_id, detail = sync_feishu_source(store, source_id=9)
        self.assertEqual("failed", status)
        self.assertIsNone(run_id)
        self.assertEqual([], store.runs)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_doc_sync_worker.py -k FeishuManualSync -v`
Expected: FAIL `AttributeError/ImportError: sync_feishu_source`

- [ ] **Step 3: 实现**

在 `sync_feishu_full.py` 中 `run_sync_feishu_full` 之前加：

```python
def sync_feishu_source(store: Any, source_id: int, mode: str = "manual") -> tuple[str, int | None, dict[str, Any]]:
    """消费一条飞书手动同步请求：doc 级=整簿重扫+全表同步；table 级=单表同步。"""
    source = store.get_source(source_id)
    if not source:
        return "failed", None, {"error": f"找不到同步源：{source_id}"}
    if source["provider"] != "feishu":
        return "failed", None, {"error": f"暂不支持该 provider：{source['provider']}"}
    if not source["external_doc_id"]:
        return "failed", None, {"error": "指定同步源缺少 app_token"}

    profile = str(source["env_profile"])
    run_id = store.start_run(provider="feishu", env_profile=profile, mode=mode)
    counts = {
        "source_count": 1,
        "sheet_count": 0,
        "record_count": 0,
        "created_count": 0,
        "updated_count": 0,
        "error_count": 0,
    }
    errors: list[dict[str, Any]] = []
    status = "failed"
    try:
        credentials = credentials_for_profile(profile)
        if not credentials:
            raise RuntimeError(f"{profile} 缺少 FEISHU_{profile}_APP_ID 或 FEISHU_{profile}_APP_SECRET。")
        credential = credentials[0]
        client = FeishuBitableClient(
            app_id=credential.app_id,
            app_secret=credential.app_secret,
            api_base=credential.api_base,
        )
        app_token = str(source["external_doc_id"])
        if not source["external_sheet_id"]:
            pairs, _disabled = _rescan_app_tables(
                store,
                client,
                profile,
                app_token,
                document_name=str(source.get("source_name") or ""),
                source_url=str(source.get("source_url") or ""),
            )
            counts["source_count"] = len(pairs)
            for table_source_id, table_source in pairs:
                try:
                    _sync_bitable_records(
                        store,
                        client,
                        table_source_id,
                        app_token,
                        table_source.table_id,
                        table_source.view_id,
                        counts,
                        source_name=table_source.sheet_name,
                    )
                except Exception as exc:  # noqa: BLE001 - 单表失败不拖垮整簿
                    counts["error_count"] += 1
                    errors.append({"source_id": table_source_id, "table_id": table_source.table_id, "error": str(exc)})
        else:
            _sync_bitable_records(
                store,
                client,
                int(source["id"]),
                app_token,
                str(source["external_sheet_id"]),
                "",
                counts,
                source_name=str(source.get("sheet_name") or source.get("source_name") or ""),
            )
        status = "success" if counts["error_count"] == 0 else "partial_failed"
    except Exception as exc:  # noqa: BLE001
        counts["error_count"] += 1
        errors.append({"source_id": source_id, "error": str(exc)})
        status = "failed"

    store.finish_run(run_id, status=status, counts=counts, error_json=errors)
    return status, run_id, {"errors": errors, "counts": counts}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_doc_sync_worker.py -k FeishuManualSync -v`
Expected: 3 PASS

- [ ] **Step 5: 提交**

```bash
git add services/doc-sync-worker/app/pipelines/sync_feishu_full.py tests/test_doc_sync_worker.py
git commit -m "feat(doc-sync): sync_feishu_source 支持飞书手动同步（doc 级整簿重扫/table 级单表）"
```

---

### Task 3: `run_sync_feishu_full` 定时全量接入整簿重扫

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`（重写 `run_sync_feishu_full` 的 sources 解析与同步循环部分）
- Test: `tests/test_doc_sync_worker.py`（`FeishuBitableSyncTests` 追加）

**Interfaces:**
- Consumes: Task 1 `_rescan_app_tables`；既有 `_merge_feishu_sources`、`_persisted_feishu_sources`、`discover_profile_sources`、`_resolve_app_token`、`ensure_bitable_app_anchor`、`store.list_registry_doc_sources`
- Produces: `run_sync_feishu_full(profiles_arg="") -> int` 行为变化——每个 app_token 先重扫再逐表同步；重扫失败回退 seed 源；单表失败记 error 继续。签名不变。

- [ ] **Step 1: 写失败测试**

```python
    def test_run_sync_feishu_full_discovers_new_tables_via_rescan(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_feishu_full as module

        class FakeClient:
            def list_tables(self, app_token: str) -> list[dict]:
                return [{"table_id": "tbl_sessions", "name": "会话索引表"}, {"table_id": "tbl_new", "name": "cs cs cs"}]

            def list_fields(self, app_token: str, table_id: str) -> list[dict]:
                return []

            def get_records(self, app_token: str, table_id: str, view_id: str = "") -> dict:
                return {"records": [], "page_count": 1}

        class FakeStore:
            def __init__(self) -> None:
                self.sources: list[dict] = []
                self.finished: dict | None = None

            def list_bitable_sources(self, provider: str, env_profile: str) -> list[dict]:
                return [
                    {
                        "external_doc_id": "bascn_console",
                        "external_sheet_id": "tbl_sessions",
                        "document_name": "飞书 ChatGPT 会话管理台",
                        "sheet_name": "会话索引表",
                        "source_url": "https://feishu.cn/base/bascn_console",
                    }
                ]

            def list_registry_doc_sources(self, provider: str, env_profile: str) -> list[dict]:
                return []

            def start_run(self, provider: str, env_profile: str, mode: str) -> int:
                return 7

            def finish_run(self, run_id: int, status: str, counts: dict, error_json: list) -> None:
                self.finished = {"status": status, "counts": dict(counts)}

            def upsert_structure_document(self, **kwargs: object) -> int:
                return 999

            def ensure_source(self, **kwargs: object) -> int:
                self.sources.append(dict(kwargs))
                return len(self.sources)

            def disable_missing_sheets(self, provider: str, env_profile: str, doc: str, seen: list) -> int:
                return 0

            def replace_fields(self, source_id: int, fields: list) -> dict:
                return {}

            def upsert_record(self, source_id: int, snapshot: object) -> object:
                raise AssertionError("no records to upsert")

            def mark_source_synced(self, source_id: int) -> None:
                return None

            def close(self) -> None:
                return None

        class Cred:
            app_id = "cli_x"
            app_secret = "s"
            api_base = "https://open.feishu.cn/open-apis"

        store = FakeStore()
        with mock.patch.object(module, "open_store", return_value=store), mock.patch.object(
            module, "env_profiles", return_value=["COMPANY_A"]
        ), mock.patch.object(module, "credentials_for_profile", return_value=[Cred()]), mock.patch.object(
            module, "FeishuBitableClient", return_value=FakeClient()
        ), mock.patch.object(module, "discover_profile_sources", return_value=[]):
            exit_code = module.run_sync_feishu_full()

        self.assertEqual(0, exit_code)
        self.assertEqual("success", store.finished["status"])
        self.assertEqual(2, store.finished["counts"]["sheet_count"])
        registered = {item["external_sheet_id"] for item in store.sources}
        self.assertIn("tbl_new", registered)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_doc_sync_worker.py -k discovers_new_tables_via_rescan -v`
Expected: FAIL（现有实现只同步已登记的 tbl_sessions，`sheet_count == 1`、`tbl_new` 未登记）

- [ ] **Step 3: 实现**

替换 `run_sync_feishu_full` 中 `for source in sources:` 循环（原 379-404 行）及其前面的 sources 收集逻辑为：

```python
                counts["source_count"] = len(sources)
                if not sources and not (
                    hasattr(store, "list_registry_doc_sources")
                    and store.list_registry_doc_sources("feishu", profile)
                ):
                    raise RuntimeError(
                        f"{profile} 未配置 FEISHU_{profile}_APP_TOKEN/TABLE_ID 或 WIKI_NODE_TOKEN，"
                        f"数据库也没有已登记 Bitable source；如需自动创建会话管理台，"
                        f"设置 FEISHU_{profile}_SESSION_CONSOLE_BOOTSTRAP=true 后运行一次同步。"
                    )

                # 1) 汇总工作簿锚点：seed 源（env+已登记表级）+ doc 级登记行（/exports/ 添加的文档）。
                anchors: dict[str, dict[str, Any]] = {}
                for source in sources:
                    app_token = _resolve_app_token(client, source)
                    anchor = anchors.setdefault(
                        app_token, {"document_name": "", "source_url": "", "view_ids": {}, "seeds": []}
                    )
                    if not anchor["document_name"]:
                        anchor["document_name"] = source.document_name or source.source_name
                    if not anchor["source_url"]:
                        anchor["source_url"] = source.source_url
                    if source.view_id:
                        anchor["view_ids"][source.table_id] = source.view_id
                    anchor["seeds"].append(source)
                if hasattr(store, "list_registry_doc_sources"):
                    for row in store.list_registry_doc_sources("feishu", profile):
                        token = str(row.get("external_doc_id") or "")
                        if token and token not in anchors:
                            anchors[token] = {
                                "document_name": str(row.get("source_name") or ""),
                                "source_url": str(row.get("source_url") or ""),
                                "view_ids": {},
                                "seeds": [],
                            }

                # 2) 每个工作簿：登记锚点 → 整簿重扫（失败回退 seed）→ 逐表同步（单表失败不拖垮整轮）。
                counts["source_count"] = 0
                for app_token, anchor in anchors.items():
                    document_name = str(anchor["document_name"] or app_token)
                    store.upsert_structure_document(
                        provider="feishu",
                        env_profile=profile,
                        source_type="bitable_app",
                        external_doc_id=app_token,
                        document_name=document_name,
                        source_url=str(anchor["source_url"]),
                    )
                    try:
                        pairs, disabled = _rescan_app_tables(
                            store,
                            client,
                            profile,
                            app_token,
                            document_name,
                            source_url=str(anchor["source_url"]),
                            view_ids=dict(anchor["view_ids"]),
                        )
                        if disabled:
                            print(f"[飞书同步] app={app_token[:6]}*** 停用已删除数据表 {disabled} 个。")
                    except Exception as exc:  # noqa: BLE001 - 重扫失败回退已登记源，别让整轮挂掉
                        print(f"[飞书同步] 整簿重扫失败，回退已登记源：{exc}")
                        pairs = []
                        for seed in anchor["seeds"]:
                            seed_id = store.ensure_source(
                                provider="feishu",
                                env_profile=profile,
                                source_name=compose_source_name(
                                    seed.document_name or seed.source_name, seed.sheet_name or seed.table_id
                                ),
                                source_type="bitable_table",
                                external_doc_id=app_token,
                                external_sheet_id=seed.table_id,
                                source_url=seed.source_url,
                                document_name=seed.document_name or seed.source_name,
                                sheet_name=seed.sheet_name or seed.table_id,
                            )
                            pairs.append((seed_id, seed))
                    counts["source_count"] += len(pairs)
                    for table_source_id, table_source in pairs:
                        try:
                            _sync_bitable_records(
                                store,
                                client,
                                table_source_id,
                                app_token,
                                table_source.table_id,
                                table_source.view_id,
                                counts,
                                source_name=table_source.sheet_name or table_source.source_name,
                            )
                        except Exception as exc:  # noqa: BLE001
                            counts["error_count"] += 1
                            errors.append(
                                {"source_id": table_source_id, "table_id": table_source.table_id, "error": str(exc)}
                            )
                status = "success" if counts["error_count"] == 0 else "partial_failed"
```

注意保留函数开头既有的 credentials 校验与 bootstrap 分支（`if not sources: bootstrap...` 逻辑保持在 anchors 收集之前，bootstrap 产生的 sources 同样进入 anchors 流程）；`ensure_bitable_app_anchor` 函数保留（bootstrap 备用），主循环改用上面内联的 `upsert_structure_document`。

- [ ] **Step 4: 跑本文件全部测试**

Run: `python -m pytest tests/test_doc_sync_worker.py -v`
Expected: 全部 PASS（含既有 bootstrap/merge/persisted 测试——若 `test_persisted...` 等测试因流程变化断言失败，修实现而非改断言；bootstrap 分支行为不得变化）

- [ ] **Step 5: 提交**

```bash
git add services/doc-sync-worker/app/pipelines/sync_feishu_full.py tests/test_doc_sync_worker.py
git commit -m "feat(doc-sync): 飞书定时全量接入整簿重扫，单表失败不拖垮整轮"
```

---

### Task 4: `run_pending_sync_requests` 按 provider 分发

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_wecom_full.py`（顶部 import + `run_pending_sync_requests` 内两处）
- Test: `tests/test_doc_sync_worker.py`（新增 `SyncRequestDispatchTests` 类）

**Interfaces:**
- Consumes: Task 2 `sync_feishu_source(store, source_id, mode) -> tuple[str, int | None, dict]`
- Produces: `run_pending_sync_requests(limit=10) -> int` 行为变化——`request["provider"] == "feishu"` 走 `sync_feishu_source`，其余走 `sync_wecom_source`；结构备份只对 wecom 请求入队。签名不变。

- [ ] **Step 1: 写失败测试**

```python
class SyncRequestDispatchTests(WorkerImportTestCase):
    def test_pending_requests_dispatch_by_provider(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_wecom_full as module

        class FakeStore:
            def __init__(self) -> None:
                self.finished: list[tuple] = []

            def pending_sync_requests(self, limit: int) -> list[dict]:
                return [
                    {"id": 1, "source_id": 1619, "provider": "feishu", "env_profile": "COMPANY_A", "mode": "manual"},
                    {"id": 2, "source_id": 100, "provider": "wecom", "env_profile": "COMPANY_B", "mode": "manual"},
                ]

            def mark_sync_request_running(self, request_id: int) -> None:
                return None

            def finish_sync_request(self, request_id: int, status: str, run_id: object, detail: dict) -> None:
                self.finished.append((request_id, status))

            def close(self) -> None:
                return None

        store = FakeStore()
        calls: list[tuple[str, int]] = []

        def fake_feishu(s: object, source_id: int, mode: str = "manual") -> tuple:
            calls.append(("feishu", source_id))
            return "success", 42, {}

        def fake_wecom(s: object, source_id: int, mode: str = "manual") -> tuple:
            calls.append(("wecom", source_id))
            return "success", 43, {}

        with mock.patch.object(module, "open_store", return_value=store), mock.patch.object(
            module, "sync_feishu_source", side_effect=fake_feishu
        ), mock.patch.object(module, "sync_wecom_source", side_effect=fake_wecom), mock.patch.object(
            module, "structure_backup_enabled", return_value=False
        ):
            exit_code = module.run_pending_sync_requests(limit=10)

        self.assertEqual(0, exit_code)
        self.assertEqual([("feishu", 1619), ("wecom", 100)], calls)
        self.assertEqual([(1, "success"), (2, "success")], store.finished)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_doc_sync_worker.py -k dispatch_by_provider -v`
Expected: FAIL（`sync_feishu_source` 不存在于 sync_wecom_full 模块 / feishu 请求走了 wecom 路径）

- [ ] **Step 3: 实现**

`sync_wecom_full.py` 顶部（`from app.providers.wecom import ...` 之后）加：

```python
from app.pipelines.sync_feishu_full import sync_feishu_source
```

`run_pending_sync_requests` 内替换（原 348-353 行）：

```python
            is_feishu = str(request.get("provider") or "") == "feishu"
            if is_feishu:
                status, run_id, detail = sync_feishu_source(store, source_id=source_id, mode="manual")
            else:
                status, run_id, detail = sync_wecom_source(store, source_id=source_id, mode="manual")
            # partial_failed（个别表受 API 限制）不视为请求失败。
            request_status = "success" if status in ("success", "partial_failed") else "failed"
            store.finish_sync_request(request_id, request_status, run_id, detail)
            if structure_backup_enabled() and not is_feishu:
                enqueue_copy_auto_structure_backup(store, request, request_status=request_status)
```

同时把开头的打印从 `[企业微信同步] 开始处理手动请求...` 改为 `[文档同步] 开始处理手动请求 request_id=... source_id=... provider=...`（带上 provider）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_doc_sync_worker.py -k dispatch_by_provider -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add services/doc-sync-worker/app/pipelines/sync_wecom_full.py tests/test_doc_sync_worker.py
git commit -m "feat(doc-sync): 手动同步请求按 provider 分发，飞书请求不再直接 failed"
```

---

### Task 5: 全量验证 + PR

**Files:**
- Modify: `CHANGELOG.md`（Unreleased 段加一行）

- [ ] **Step 1: 全量根测试**

Run: `python -m pytest tests/ -q`
Expected: 全部 PASS（无回归）

- [ ] **Step 2: CHANGELOG**

`CHANGELOG.md` Unreleased 段追加：

```markdown
- doc-sync-worker：飞书多维表格支持手动同步（此前一律「暂不支持该 provider」失败），定时/手动同步均整簿重扫——新建数据表自动收录进 /exports/，删除的表标记停用。
```

- [ ] **Step 3: 提交并建 PR**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for feishu manual sync + doc rescan"
git push -u origin feature/feishu-manual-sync-rescan
gh pr create --title "doc-sync: 飞书手动同步 + 整簿重扫" --body "..."
```

PR body 说明根因（sync_requests 全 failed 的生产证据）与行为变化；合并后 release-deploy 自动部署 doc-sync-worker。

- [ ] **Step 4: 部署后生产验证**

1. `/exports/` 点"同步数据列表"→ `sync_requests` 最新 feishu 行 status=success；
2. 飞书页签出现 8 张表（含「使用说明」「cs cs cs」）且 rows 有值；
3. `sync_runs` 出现 provider=feishu mode=manual success。
