# bom-builder 新建存货流程/建议编码/查重/响应式 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** bom-builder 新建父件/原料时提供完整存货选项（属性 6 项）、按分类建议编码、本地查重与 T+ 报错全文透传，并做桌面两栏响应式。

**Architecture:** 后端在 `tplus_bom.py` router 加建议编码端点、给 `/inventories` 加 `include_disabled`；领域层 `app/tplus_bom.py` 属性字段进 dto（缺键回退旧逻辑）；worker 侧 `ChanjetClient` 解析 T+ 业务错误原文并区分"确定拒绝→failed / 结果不确定→needs_review"；前端单文件 `index.html` 重组父件区为三分组并加建议编码/查重条。

**Tech Stack:** FastAPI + pydantic + pandas（读每日存货导出 xlsx）；原生 JS 单页；pytest + unittest。

## Global Constraints

- spec：`docs/superpowers/specs/2026-07-13-bom-builder-inventory-create-design.md`
- 建议编码规则 = 分类编码**前 2 位** + **6 位流水**（`^PP\d{6}$`），扫描每日导出取最大流水 +1，无匹配从 `PP000001` 起。历史杂乱编码一律不管。
- 存货属性 T+ 字段名（实测验证过）：外购=`IsPurchase`、销售=`IsSale`、自制=`IsMadeSelf`、生产耗用=`IsMaterial`、委外=`IsMadeRequest`、虚拟件=`IsPhantom`。
- 前端默认：父件属性勾 5 项（虚拟件不勾）；原料弹窗默认只勾 外购+生产耗用；所属类别默认"物料清单"、单位默认"kg"（沿用现有常量）。
- 旧草稿兼容：item dict 缺属性键时 `build_inventory_create_payload` 回退旧的按 kind 写死逻辑（parent=销售+自制，material=外购+生产耗用），**回退时不新增 IsMadeRequest/IsPhantom 键**。
- 查重/建议接口失败不阻塞录入，只提示；重复编码在前端阻断提交。
- backend 测试：仓库根 `python -m pytest tests/ -q`；worker 测试：`services/tplus-sync-worker` 下 `PYTHONPATH=src python -m pytest tests/ -q`（Windows PowerShell 用 `$env:PYTHONPATH='src'`）。
- 不新增依赖、不加 DB 迁移（草稿 JSON 自由扩展）。
- git 提交只显式 add 本任务列出的文件，禁 `-A`/`.`/`-u`。

---

### Task 1: 建议编码端点 `GET /v1/tplus/inventory-code-suggestion`

**Files:**
- Modify: `services/backend-api/app/routers/tplus_bom.py`（在 `tplus_inventory_choices` 之后加端点；文件头 import 区加 `import re`）
- Test: `tests/test_backend_tplus_bom_picker.py`

**Interfaces:**
- Produces: `GET /v1/tplus/inventory-code-suggestion?class_code=<str>` → `{"suggested": "06000001", "prefix": "06", "source_file": "..."}`；错误：400（前缀非两位数字）、404（导出缺失）、409（编码列缺失/流水耗尽）。前端 Task 6 消费。

- [ ] **Step 1: 写失败测试**

在 `tests/test_backend_tplus_bom_picker.py` 追加（文件末尾、`if __name__` 之前；新类复用既有 setUp 模式）：

```python
def _write_inventory_for_suggestion(directory: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["Code", "Name", "Specification", "BaseUnitCode", "BaseUnitName", "Disabled"])
    ws.append(["06000009", "旧父件九", "", "1", "kg", "False"])
    ws.append(["06000012", "旧父件十二", "", "1", "kg", "False"])
    ws.append(["0316-CO712", "历史杂码", "", "1", "kg", "False"])
    ws.append(["069999", "位数不足不算", "", "1", "kg", "False"])
    ws.append(["01000030", "原料", "", "1", "kg", "False"])
    wb.save(directory / "inventory_20260713_010000.xlsx")


class TPlusCodeSuggestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(BACKEND_ROOT))
        from app.routers import tplus_bom as module
        cls.main = module

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path[:] = [item for item in sys.path if item != str(BACKEND_ROOT)]

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = os.environ.get("TPLUS_EXPORT_DIR")
        os.environ["TPLUS_EXPORT_DIR"] = self.tmp.name
        _write_inventory_for_suggestion(Path(self.tmp.name))

    def tearDown(self) -> None:
        if self.old_dir is None:
            os.environ.pop("TPLUS_EXPORT_DIR", None)
        else:
            os.environ["TPLUS_EXPORT_DIR"] = self.old_dir
        self.tmp.cleanup()

    def _user(self) -> dict:
        return {"sub": "tester", "roles": [], "permissions": ["tplus.bom.write"]}

    def test_suggests_max_serial_plus_one_for_prefix(self):
        result = self.main.tplus_inventory_code_suggestion(class_code="06", user=self._user())
        self.assertEqual("06000013", result["suggested"])
        self.assertEqual("06", result["prefix"])

    def test_first_code_when_prefix_unused(self):
        result = self.main.tplus_inventory_code_suggestion(class_code="09", user=self._user())
        self.assertEqual("09000001", result["suggested"])

    def test_subclass_code_uses_first_two_digits(self):
        # 末级分类 1201 → 前缀 12
        result = self.main.tplus_inventory_code_suggestion(class_code="1201", user=self._user())
        self.assertEqual("12000001", result["suggested"])

    def test_rejects_non_numeric_prefix(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.main.tplus_inventory_code_suggestion(class_code="AB", user=self._user())
        self.assertEqual(400, ctx.exception.status_code)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backend_tplus_bom_picker.py -q`
Expected: FAIL，`AttributeError: ... has no attribute 'tplus_inventory_code_suggestion'`

- [ ] **Step 3: 实现端点**

`services/backend-api/app/routers/tplus_bom.py`：`from __future__` 后 import 区加 `import re`；在 `tplus_inventory_choices` 函数之后加：

```python
CODE_SERIAL_WIDTH = 6
CODE_SERIAL_MAX = 10 ** CODE_SERIAL_WIDTH - 1


@router.get("/inventory-code-suggestion")
def tplus_inventory_code_suggestion(
    class_code: str = Query(min_length=2, max_length=100),
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    """按用户约定：分类编码前 2 位 + 6 位不重复流水；历史杂乱编码不参与。"""
    _require_bom_write(user)
    prefix = class_code.strip()[:2]
    if not re.fullmatch(r"\d{2}", prefix):
        raise HTTPException(status_code=400, detail="分类编码前两位必须是数字")
    path = _latest_tplus_export_file("inventory")
    if path is None:
        raise HTTPException(status_code=404, detail="存货档案尚未同步")
    import pandas as pd

    df = pd.read_excel(path, dtype=str).fillna("")
    code_col = _inventory_column(df, "Code", "InventoryCode", "存货编码")
    if not code_col:
        raise HTTPException(status_code=409, detail="存货档案缺少编码字段")
    pattern = re.compile(rf"^{prefix}(\d{{{CODE_SERIAL_WIDTH}}})$")
    serials = [
        int(match.group(1))
        for code in df[code_col].astype(str).str.strip()
        if (match := pattern.fullmatch(code))
    ]
    next_serial = (max(serials) + 1) if serials else 1
    if next_serial > CODE_SERIAL_MAX:
        raise HTTPException(status_code=409, detail="该类别流水号已用尽，请人工定义编码")
    return {
        "suggested": f"{prefix}{next_serial:0{CODE_SERIAL_WIDTH}d}",
        "prefix": prefix,
        "source_file": path.name,
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backend_tplus_bom_picker.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add services/backend-api/app/routers/tplus_bom.py tests/test_backend_tplus_bom_picker.py
git commit -m "feat(tplus): 按分类前缀建议下一个存货编码端点"
```

---

### Task 2: `/inventories` 加 `include_disabled` 参数（查重需覆盖停用编码）

**Files:**
- Modify: `services/backend-api/app/routers/tplus_bom.py`（`tplus_inventory_choices` 签名与停用过滤段）
- Test: `tests/test_backend_tplus_bom_picker.py`

**Interfaces:**
- Produces: `GET /v1/tplus/inventories?...&include_disabled=true` → 结果包含停用存货。前端 Task 6 查重时带 `include_disabled=true`。默认 false，既有行为不变。

- [ ] **Step 1: 写失败测试**

`tests/test_backend_tplus_bom_picker.py` 的 `_write_inventory` 追加一行停用存货（`wb.save` 之前）：

```python
    ws.append(["06000088", "停用旧父件", "", "1", "kg", "True"])
```

`TPlusBomPickerTests` 类内追加：

```python
    def test_disabled_rows_hidden_by_default(self):
        result = self.main.tplus_inventory_choices(q="06000088", limit=20, scope="all", user=self._user())
        self.assertEqual(0, result["total"])

    def test_include_disabled_reveals_disabled_rows(self):
        result = self.main.tplus_inventory_choices(
            q="06000088", limit=20, scope="all", include_disabled=True, user=self._user()
        )
        self.assertEqual(["06000088"], [item["code"] for item in result["items"]])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backend_tplus_bom_picker.py -q`
Expected: `test_include_disabled_reveals_disabled_rows` FAIL（unexpected keyword argument）

- [ ] **Step 3: 实现**

`tplus_inventory_choices` 签名在 `scope` 参数后加：

```python
    include_disabled: bool = Query(default=False),
```

停用过滤段改为：

```python
    if disabled_col and not include_disabled:
        disabled = df[disabled_col].astype(str).str.strip().str.lower()
        df = df[~disabled.isin({"1", "true", "yes", "是"})]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backend_tplus_bom_picker.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add services/backend-api/app/routers/tplus_bom.py tests/test_backend_tplus_bom_picker.py
git commit -m "feat(tplus): inventories 支持 include_disabled 供编码查重"
```

---

### Task 3: 存货属性进领域模型与创建 payload

**Files:**
- Modify: `services/backend-api/app/tplus_bom.py`（`_validate_custom_inventory`、`build_inventory_create_payload`）
- Modify: `services/backend-api/app/routers/tplus_bom.py`（`BomParent` 模型）
- Test: `tests/test_tplus_bom_write.py`

**Interfaces:**
- Consumes: 无（独立于 Task 1/2）。
- Produces: `BomParent`/`BomChild` 新增 6 个可空布尔字段 `is_purchase / is_sale / is_made_self / is_material / is_made_request / is_phantom`（默认 `None`=未提供）；`build_inventory_create_payload(item, kind=...)` 在 item 含任一属性键非 None 时按属性生成 dto 的 `IsPurchase/IsSale/IsMadeSelf/IsMaterial/IsMadeRequest/IsPhantom`，全缺时回退旧 kind 逻辑（且不含后两个键）。前端 Task 5 提交这些字段。

- [ ] **Step 1: 写失败测试**

`tests/test_tplus_bom_write.py` 类内追加：

```python
    def test_explicit_attributes_override_kind_defaults(self):
        item = {
            "source": "custom", "code": "06000013", "name": "新父件",
            "inventory_class_code": "06", "inventory_class_name": "物料清单",
            "unit_code": "1", "unit_name": "kg",
            "is_purchase": True, "is_sale": True, "is_made_self": True,
            "is_material": True, "is_made_request": True, "is_phantom": False,
        }
        dto = main.build_inventory_create_payload(item, kind="parent")["dto"]
        self.assertTrue(dto["IsPurchase"])
        self.assertTrue(dto["IsMaterial"])
        self.assertTrue(dto["IsMadeRequest"])
        self.assertFalse(dto["IsPhantom"])

    def test_legacy_item_without_attribute_keys_keeps_old_kind_defaults(self):
        item = {
            "source": "custom", "code": "RM-NEW", "name": "新原料",
            "inventory_class_code": "01", "inventory_class_name": "原材料",
            "unit_code": "1", "unit_name": "kg",
        }
        dto = main.build_inventory_create_payload(item, kind="material")["dto"]
        self.assertTrue(dto["IsPurchase"])
        self.assertTrue(dto["IsMaterial"])
        self.assertFalse(dto["IsSale"])
        self.assertFalse(dto["IsMadeSelf"])
        self.assertNotIn("IsMadeRequest", dto)
        self.assertNotIn("IsPhantom", dto)

    def test_all_false_attributes_rejected(self):
        parent = {
            "source": "custom", "code": "06000013", "name": "新父件",
            "inventory_class_code": "06", "inventory_class_name": "物料清单",
            "unit_code": "1", "unit_name": "kg",
            "is_purchase": False, "is_sale": False, "is_made_self": False,
            "is_material": False, "is_made_request": False, "is_phantom": False,
        }
        _, children, options = self._draft()
        errors = main.validate_bom_draft(parent, children, options)
        self.assertTrue(any("至少勾选一项存货属性" in error for error in errors))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_tplus_bom_write.py -q`
Expected: 三个新测试 FAIL（KeyError `IsMadeRequest` / 无"至少勾选"报错）

- [ ] **Step 3: 实现领域层**

`services/backend-api/app/tplus_bom.py`：模块级（`_text` 之后）加：

```python
ATTRIBUTE_FIELDS: tuple[tuple[str, str], ...] = (
    ("is_purchase", "IsPurchase"),
    ("is_sale", "IsSale"),
    ("is_made_self", "IsMadeSelf"),
    ("is_material", "IsMaterial"),
    ("is_made_request", "IsMadeRequest"),
    ("is_phantom", "IsPhantom"),
)


def _attribute_flags(item: dict[str, Any], kind: str) -> dict[str, bool]:
    """属性键全缺（旧草稿）时回退旧 kind 写死逻辑，且不引入新键。"""
    if all(item.get(key) is None for key, _ in ATTRIBUTE_FIELDS):
        is_parent = kind == "parent"
        return {
            "IsPurchase": not is_parent,
            "IsSale": is_parent,
            "IsMadeSelf": is_parent,
            "IsMaterial": not is_parent,
        }
    return {dto_key: bool(item.get(key)) for key, dto_key in ATTRIBUTE_FIELDS}
```

`_validate_custom_inventory` 的 `for` 循环之后、`return errors` 之前加：

```python
    provided = [item.get(key) for key, _ in ATTRIBUTE_FIELDS if item.get(key) is not None]
    if provided and not any(provided):
        errors.append(f"{label}至少勾选一项存货属性")
```

`build_inventory_create_payload` 中删除写死的四行：

```python
        "IsPurchase": not is_parent,
        "IsSale": is_parent,
        "IsMadeSelf": is_parent,
        "IsMaterial": not is_parent,
```

（同时删掉不再使用的 `is_parent = kind == "parent"`），并在 `dto` 字面量之后、`return` 之前加：

```python
    dto.update(_attribute_flags(item, kind))
```

- [ ] **Step 4: router 模型加字段**

`services/backend-api/app/routers/tplus_bom.py` 的 `BomParent` 类（`inventory_class_name` 之后）加：

```python
    is_purchase: bool | None = None
    is_sale: bool | None = None
    is_made_self: bool | None = None
    is_material: bool | None = None
    is_made_request: bool | None = None
    is_phantom: bool | None = None
```

（`BomChild` 继承自动获得。）

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_tplus_bom_write.py tests/test_backend_tplus_bom_picker.py -q`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add services/backend-api/app/tplus_bom.py services/backend-api/app/routers/tplus_bom.py tests/test_tplus_bom_write.py
git commit -m "feat(tplus): 存货属性六项进创建 payload，旧草稿回退 kind 默认"
```

---

### Task 4: worker T+ 业务报错原文透传 + 确定拒绝转 failed

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/chanjet/client.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/core/exceptions.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/bom_write_worker.py`
- Test: `services/tplus-sync-worker/tests/test_bom_write_worker.py`、`services/tplus-sync-worker/tests/test_chanjet_client.py`（若无此文件则新建）

**Interfaces:**
- Produces: `ChanjetAPIError` 新增属性 `business_message: str`（T+ 返回体解析出的业务错误原文，如"存货编号：30122027-3027不唯一，请尝试修改该编号中的流水号后再操作"）；`str(exc)` 含该原文。worker：凡 `ChanjetAPIError.business_message` 非空 → T+ 确定拒绝 → `failed`（错误原文入 `error_json.message`）；网络异常/无业务文本维持 `needs_review`。前端无需改（已渲染 `error.message`）。

- [ ] **Step 1: 写失败测试（client 解析）**

新建 `services/tplus-sync-worker/tests/test_chanjet_client.py`：

```python
from __future__ import annotations

import unittest

from tplus_datahub.chanjet.client import ChanjetClient
from tplus_datahub.core.exceptions import ChanjetAPIError


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json
        return json.loads(self.text)


class FakeSession:
    def __init__(self, response):
        self.response = response

    def post(self, *args, **kwargs):
        return self.response


class FakeSettings:
    base_url = "https://openapi.example"
    timeout = 5
    app_key = "k"
    app_secret = "s"
    open_token = "t"


class ChanjetClientErrorTests(unittest.TestCase):
    def _client(self, response):
        return ChanjetClient(settings=FakeSettings(), session=FakeSession(response))

    def test_http_error_surfaces_business_message(self):
        body = '{"message": "存货编号：30122027-3027不唯一，请尝试修改该编号中的流水号后再操作"}'
        client = self._client(FakeResponse(500, body))
        with self.assertRaises(ChanjetAPIError) as ctx:
            client.post("/tplus/api/v2/inventory/Create", {"dto": {}})
        self.assertIn("不唯一", ctx.exception.business_message)
        self.assertIn("不唯一", str(ctx.exception))

    def test_http_error_nested_message_found(self):
        body = '{"result": {"Exception": {"Message": "父级错误包裹"}}}'
        client = self._client(FakeResponse(400, body))
        with self.assertRaises(ChanjetAPIError) as ctx:
            client.post("/x", {})
        self.assertEqual("父级错误包裹", ctx.exception.business_message)

    def test_http_error_without_json_body_keeps_generic_message(self):
        client = self._client(FakeResponse(502, "<html>bad gateway</html>"))
        with self.assertRaises(ChanjetAPIError) as ctx:
            client.post("/x", {})
        self.assertEqual("", ctx.exception.business_message)
        self.assertIn("HTTP 502", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
```

注意：若 `build_auth_headers(FakeSettings())` 取更多字段导致报错，按其真实签名给 FakeSettings 补齐属性（以现有 `services/tplus-sync-worker/src/config/settings.py` 为准），不改动 client 逻辑。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/tplus-sync-worker && PYTHONPATH=src python -m pytest tests/test_chanjet_client.py -q`
（PowerShell：`$env:PYTHONPATH='src'; python -m pytest tests/test_chanjet_client.py -q`）
Expected: FAIL（`ChanjetAPIError` 无 `business_message` / 构造缺参数）

- [ ] **Step 3: 实现 client + exception**

`exceptions.py` 的 `ChanjetAPIError.__init__` 改为：

```python
class ChanjetAPIError(TPlusDataHubError):
    def __init__(
        self,
        message: str,
        endpoint: str,
        status_code: int | None = None,
        body_preview: str = "",
        business_message: str = "",
    ):
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code
        self.body_preview = body_preview
        self.business_message = business_message
```

`client.py` 模块级加（import 区加 `import json`）：

```python
_MESSAGE_KEYS = ("message", "Message", "msg", "Msg", "error", "Error", "detail", "Detail")


def _business_message(text: str) -> str:
    """从 T+ 错误返回体提取业务错误原文；解析不了返回空串。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return ""

    def walk(node: Any) -> str:
        if isinstance(node, dict):
            for key in _MESSAGE_KEYS:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in node.values():
                if isinstance(value, (dict, list)):
                    nested = walk(value)
                    if nested:
                        return nested
        elif isinstance(node, list):
            for value in node:
                nested = walk(value)
                if nested:
                    return nested
        return ""

    return walk(data)
```

`post()` 中 HTTP >=400 分支改为：

```python
        if status_code is not None and status_code >= 400:
            raw_text = getattr(response, "text", "")
            business = _business_message(raw_text)
            message = f"T+ 返回错误：{business}" if business else f"接口返回 HTTP {status_code}"
            raise ChanjetAPIError(
                message=message,
                endpoint=endpoint,
                status_code=status_code,
                body_preview=text_preview(raw_text, 1000),
                business_message=business,
            )
```

- [ ] **Step 4: 写失败测试（worker 分流）**

`services/tplus-sync-worker/tests/test_bom_write_worker.py` import 区加：

```python
from tplus_datahub.core.exceptions import ChanjetAPIError
```

类内追加：

```python
    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_tplus_business_rejection_is_definite_failure(self, finish, _event):
        submission = self._submission()
        submission["request_json"] = {
            "bom": submission["request_json"],
            "custom_inventories": [{
                "kind": "parent", "code": "06000013",
                "payload": {"dto": {"Code": "06000013", "Name": "新父件"}},
            }],
        }
        rejection = ChanjetAPIError(
            "T+ 返回错误：存货编号：06000013不唯一，请尝试修改该编号中的流水号后再操作",
            endpoint="/inv/Create", status_code=500,
            business_message="存货编号：06000013不唯一，请尝试修改该编号中的流水号后再操作",
        )
        client = FakeClient([[], [], rejection])
        status = bom_write_worker.process_submission(submission, client=client)
        self.assertEqual("failed", status)
        self.assertIn("不唯一", finish.call_args.kwargs["error"]["message"])

    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_bom_create_business_rejection_is_definite_failure(self, finish, _event):
        rejection = ChanjetAPIError(
            "T+ 返回错误：BOM 数据不合法", endpoint="/bom/Create",
            status_code=500, business_message="BOM 数据不合法",
        )
        client = FakeClient([[], rejection])
        status = bom_write_worker.process_submission(self._submission(), client=client)
        self.assertEqual("failed", status)
        self.assertIn("BOM 数据不合法", finish.call_args.kwargs["error"]["message"])
```

- [ ] **Step 5: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_bom_write_worker.py -q`
Expected: 两个新测试 FAIL（现在都是 needs_review）

- [ ] **Step 6: 实现 worker 分流**

`bom_write_worker.py` import 区加：

```python
from tplus_datahub.core.exceptions import ChanjetAPIError
```

`process_submission` 自定义存货循环中，`except ValueError` 之前插入：

```python
        except ChanjetAPIError as exc:
            if exc.business_message:
                # T+ 明确拒绝（返回了业务错误文本），是确定失败，原文透传给用户。
                finish_submission(
                    submission_id, status="failed", verification={"inventories": prepared},
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
                return "failed"
            finish_submission(
                submission_id, status="needs_review", verification={"inventories": prepared},
                error={"type": type(exc).__name__, "message": f"自定义存货处理结果不确定：{exc}"},
            )
            return "needs_review"
```

BOM create 的 `except Exception as exc:` 之前插入：

```python
    except ChanjetAPIError as exc:
        if exc.business_message:
            finish_submission(
                submission_id, status="failed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            return "failed"
        finish_submission(
            submission_id, status="needs_review",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        return "needs_review"
```

- [ ] **Step 7: 跑 worker 全部测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add services/tplus-sync-worker/src/tplus_datahub/chanjet/client.py services/tplus-sync-worker/src/tplus_datahub/core/exceptions.py services/tplus-sync-worker/src/tplus_datahub/jobs/bom_write_worker.py services/tplus-sync-worker/tests/test_bom_write_worker.py services/tplus-sync-worker/tests/test_chanjet_client.py
git commit -m "feat(tplus-worker): T+ 业务报错原文透传，确定拒绝转 failed"
```

---

### Task 5: 前端父件区三分组 + 存货属性复选框

**Files:**
- Modify: `services/public-web/bom-builder/index.html`
- Test: `tests/test_bom_builder_page.py`、`tests/test_tplus_bom_builder_frontend.py`

**Interfaces:**
- Consumes: Task 3 的 `is_*` 字段（提交进 parent/自定义子件 JSON）。
- Produces: DOM 锚点 `parentAttrBox`、`customAttrBox`（内含 6 个 `input[data-attr]`）；JS 函数 `attrValues(box)` → `{is_purchase:bool,...}`（Task 6 不依赖，Task 7 不依赖，仅本任务内使用）。

- [ ] **Step 1: 写失败测试**

`tests/test_bom_builder_page.py` 追加：

```python
def test_parent_new_mode_grouped_with_attributes():
    html = read_page()
    # 三分组标题
    for title in ("基本信息", "计量单位", "存货属性"):
        assert title in html, title
    assert 'id="parentAttrBox"' in html
    assert 'id="customAttrBox"' in html
    # 6 个属性复选框锚点
    for attr in ("is_purchase", "is_sale", "is_made_self", "is_material", "is_made_request", "is_phantom"):
        assert f'data-attr="{attr}"' in html, attr


def test_parent_attribute_defaults_five_checked_phantom_off():
    html = read_page()
    parent_box = html.split('id="parentAttrBox"')[1].split("</details>")[0]
    for attr in ("is_purchase", "is_sale", "is_made_self", "is_material", "is_made_request"):
        assert f'data-attr="{attr}" checked' in parent_box, attr
    assert 'data-attr="is_phantom" checked' not in parent_box


def test_custom_material_attribute_defaults_purchase_and_material_only():
    html = read_page()
    custom_box = html.split('id="customAttrBox"')[1].split("</div></div>")[0]
    assert 'data-attr="is_purchase" checked' in custom_box
    assert 'data-attr="is_material" checked' in custom_box
    for attr in ("is_sale", "is_made_self", "is_made_request", "is_phantom"):
        assert f'data-attr="{attr}" checked' not in custom_box, attr
```

`tests/test_tplus_bom_builder_frontend.py` 的 `test_page_supports_material_scope_quantity_and_custom_inventory` 内追加断言：

```python
        self.assertIn("is_made_request", self.html)
        self.assertIn("attrValues", self.html)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_bom_builder_page.py tests/test_tplus_bom_builder_frontend.py -q`
Expected: 新测试 FAIL

- [ ] **Step 3: 实现 HTML 结构**

`index.html` CSS 区（`details.adv` 行后）加：

```css
    details.group{border:1px solid var(--line);border-radius:12px;padding:8px 12px;margin-top:10px}
    details.group summary{cursor:pointer;font-weight:650;font-size:14px;padding:4px 0}
    .attr-row{display:flex;flex-wrap:wrap;gap:10px 16px;margin-top:8px}
    .attr-row label{display:flex;align-items:center;gap:5px;font-size:14px;color:var(--text)}
    .attr-row input{width:auto}
```

`parentNewBox` 整块替换为（原编码/名称/类别/单位四个 field 移入分组；编码 field 下加提示/警告两行，供 Task 6 使用）：

```html
      <div id="parentNewBox">
        <details class="group" open><summary>基本信息</summary>
          <div class="grid" style="margin-top:8px">
            <div class="field"><label for="parentCode">父件编码 *</label><input id="parentCode" maxlength="100" autocomplete="off"/>
              <div id="parentCodeHint" class="small muted"></div>
              <div id="parentCodeWarn" class="small" style="color:var(--danger)"></div></div>
            <div class="field"><label for="parentName">父件名称 *</label><input id="parentName" maxlength="200" autocomplete="off"/></div>
            <div class="field"><label for="parentClassSelect">所属类别 *</label><select id="parentClassSelect"><option value="">读取 T+ 分类中…</option></select></div>
          </div>
        </details>
        <details class="group" open><summary>计量单位</summary>
          <div class="grid" style="margin-top:8px">
            <div class="field"><label for="parentUnitSelect">单位 *</label><select id="parentUnitSelect"><option value="">读取单位中…</option></select></div>
          </div>
        </details>
        <details class="group" open><summary>存货属性</summary>
          <div class="attr-row" id="parentAttrBox">
            <label><input type="checkbox" data-attr="is_purchase" checked/> 外购</label>
            <label><input type="checkbox" data-attr="is_sale" checked/> 销售</label>
            <label><input type="checkbox" data-attr="is_made_self" checked/> 自制</label>
            <label><input type="checkbox" data-attr="is_material" checked/> 生产耗用</label>
            <label><input type="checkbox" data-attr="is_made_request" checked/> 委外</label>
            <label><input type="checkbox" data-attr="is_phantom"/> 虚拟件</label>
          </div>
        </details>
      </div>
```

`customModal` 的 grid 结束标签 `</div>` 之后、按钮行之前加（编码 field 内同样加 hint/warn 两行 `customCodeHint`/`customCodeWarn`，即把 `customCode` 的 field 改为带两个 div，同 parentCode 样式）：

```html
    <div class="field wide" style="margin-top:8px"><label>存货属性</label>
      <div class="attr-row" id="customAttrBox">
        <label><input type="checkbox" data-attr="is_purchase" checked/> 外购</label>
        <label><input type="checkbox" data-attr="is_sale"/> 销售</label>
        <label><input type="checkbox" data-attr="is_made_self"/> 自制</label>
        <label><input type="checkbox" data-attr="is_material" checked/> 生产耗用</label>
        <label><input type="checkbox" data-attr="is_made_request"/> 委外</label>
        <label><input type="checkbox" data-attr="is_phantom"/> 虚拟件</label>
      </div></div>
```

- [ ] **Step 4: 实现 JS 接线**

script 区加（`pickOption` 附近）：

```js
    function attrValues(box){const out={};box.querySelectorAll('[data-attr]').forEach((el)=>{out[el.dataset.attr]=el.checked});return out;}
    function restoreAttrs(box,saved){if(!saved)return;box.querySelectorAll('[data-attr]').forEach((el)=>{if(saved[el.dataset.attr]!==undefined)el.checked=!!saved[el.dataset.attr]});}
```

`parentItem()` 的 return（new 分支）改为在对象末尾展开属性：

```js
      return{source:'custom',code:$('parentCode').value.trim(),name:$('parentName').value.trim(),specification:$('parentSpec').value.trim(),inventory_class_code:classOption?.value||'',inventory_class_name:classOption?.dataset.name||'',unit_code:unitOption?.value||'',unit_name:unitOption?.dataset.name||'',...attrValues($('parentAttrBox'))};
```

`addCustom()` 的 `item` 构造同样加 `,...attrValues($('customAttrBox'))`；其必填校验循环后加：

```js
      if(!Object.values(attrValues($('customAttrBox'))).some(Boolean)){message('新增原料至少勾选一项存货属性。');return}
```

`localCheck()` 的 new 分支加：

```js
        if(!Object.values(attrValues($('parentAttrBox'))).some(Boolean))throw new Error('父件至少勾选一项存货属性。');
```

`snapshot()` 的 `form` 对象加 `parentAttrs:attrValues($('parentAttrBox'))`；`restore()` 表单回填处加 `restoreAttrs($('parentAttrBox'),form.parentAttrs);`。
装配区 `markDirty` 监听补上属性框：

```js
      $('parentAttrBox').querySelectorAll('[data-attr]').forEach((el)=>el.addEventListener('change',markDirty));
```

`openCustomModal` 重置默认勾选（`pickOption` 两行之后）：

```js
      $('customAttrBox').querySelectorAll('[data-attr]').forEach((el)=>{el.checked=['is_purchase','is_material'].includes(el.dataset.attr)});
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_bom_builder_page.py tests/test_tplus_bom_builder_frontend.py -q`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add services/public-web/bom-builder/index.html tests/test_bom_builder_page.py tests/test_tplus_bom_builder_frontend.py
git commit -m "feat(bom-builder): 父件区三分组+存货属性复选框（父件默认5项/原料默认2项）"
```

---

### Task 6: 建议编码提示条 + 编码失焦查重

**Files:**
- Modify: `services/public-web/bom-builder/index.html`
- Test: `tests/test_bom_builder_page.py`、`tests/test_tplus_bom_builder_frontend.py`

**Interfaces:**
- Consumes: Task 1 端点 `GET /v1/tplus/inventory-code-suggestion?class_code=`；Task 2 参数 `include_disabled=true`；Task 5 的 `parentCodeHint/parentCodeWarn/customCodeHint/customCodeWarn` 锚点。
- Produces: JS `dupState` 对象（`{parentCode:bool, customCode:bool}`），`localCheck`/`addCustom` 依据它阻断。

- [ ] **Step 1: 写失败测试**

`tests/test_bom_builder_page.py` 追加：

```python
def test_code_suggestion_and_duplicate_check_wiring():
    html = read_page()
    assert "/v1/tplus/inventory-code-suggestion" in html
    assert "include_disabled=true" in html
    assert 'id="parentCodeHint"' in html and 'id="parentCodeWarn"' in html
    assert 'id="customCodeHint"' in html and 'id="customCodeWarn"' in html
    assert "编码已存在" in html
    assert "dupState" in html
```

`tests/test_tplus_bom_builder_frontend.py` 追加断言（同一测试方法内）：

```python
        self.assertIn("inventory-code-suggestion", self.html)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_bom_builder_page.py tests/test_tplus_bom_builder_frontend.py -q`
Expected: 新断言 FAIL

- [ ] **Step 3: 实现 JS**

script 区加（`attachSearch` 之后）：

```js
    // ---------- 建议编码与查重（辅助功能：接口失败不阻塞录入） ----------
    const dupState={parentCode:false,customCode:false};
    async function refreshCodeSuggestion(selectEl,hintEl,inputEl){
      hintEl.textContent='';
      const cls=selectEl.value;if(!cls||!token())return;
      try{const data=await api(`/v1/tplus/inventory-code-suggestion?class_code=${encodeURIComponent(cls)}`);
        hintEl.innerHTML=`建议编码：<button type="button" class="linklike" data-suggest>${esc(data.suggested)}</button>（点击填入，避免与已有编码重复）`;
        hintEl.querySelector('[data-suggest]').onclick=()=>{inputEl.value=data.suggested;inputEl.dispatchEvent(new Event('input',{bubbles:true}));};}
      catch(e){hintEl.textContent=`建议编码不可用：${e.message}`;}}
    async function checkCodeDuplicate(inputEl,warnEl){
      const key=inputEl.id,code=inputEl.value.trim();
      warnEl.textContent='';dupState[key]=false;
      if(!code||!token())return;
      try{const data=await api(`/v1/tplus/inventories?q=${encodeURIComponent(code)}&limit=50&scope=all&include_disabled=true`);
        if(inputEl.value.trim()!==code)return;
        const hit=(data.items||[]).find((x)=>x.code===code);
        if(hit){dupState[key]=true;warnEl.textContent=`编码已存在：${hit.name}（请修改流水号）`;}}
      catch{warnEl.textContent='查重暂不可用（提交时以 T+ 校验为准）';}}
    function wireCodeChecks(inputEl,warnEl){
      const run=debounce(()=>checkCodeDuplicate(inputEl,warnEl),400);
      inputEl.addEventListener('input',run);
      inputEl.addEventListener('blur',()=>checkCodeDuplicate(inputEl,warnEl));}
```

`localCheck()` 的 new 分支再加：

```js
        if(dupState.parentCode)throw new Error('父件编码已存在，请修改流水号或点击建议编码。');
```

`addCustom()` 属性校验后加：

```js
      if(dupState.customCode){message('新增原料编码已存在，请修改流水号或点击建议编码。');return}
```

装配区（DOMContentLoaded 内）加：

```js
      wireCodeChecks($('parentCode'),$('parentCodeWarn'));
      wireCodeChecks($('customCode'),$('customCodeWarn'));
      $('parentClassSelect').addEventListener('change',()=>refreshCodeSuggestion($('parentClassSelect'),$('parentCodeHint'),$('parentCode')));
      $('customClassSelect').addEventListener('change',()=>refreshCodeSuggestion($('customClassSelect'),$('customCodeHint'),$('customCode')));
```

`ensureOptions().then(...)` 成功回调里（pending 回填之后）加 `refreshCodeSuggestion($('parentClassSelect'),$('parentCodeHint'),$('parentCode'));`；`openCustomModal` 末尾（`focus()` 之前）加 `refreshCodeSuggestion($('customClassSelect'),$('customCodeHint'),$('customCode'));$('customCodeWarn').textContent='';dupState.customCode=false;`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_bom_builder_page.py tests/test_tplus_bom_builder_frontend.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add services/public-web/bom-builder/index.html tests/test_bom_builder_page.py tests/test_tplus_bom_builder_frontend.py
git commit -m "feat(bom-builder): 分类建议编码点击填入 + 编码失焦查重阻断"
```

---

### Task 7: 桌面两栏响应式 + 提交状态卡存货事件摘要

**Files:**
- Modify: `services/public-web/bom-builder/index.html`
- Test: `tests/test_bom_builder_page.py`

**Interfaces:**
- Consumes: 现有 `pollSubmission` 的 `data.events`（worker 已写 `inventory_created`/`inventory_reused` 事件）。
- Produces: 无对外接口。

- [ ] **Step 1: 写失败测试**

`tests/test_bom_builder_page.py` 追加：

```python
def test_desktop_two_column_layout_and_inventory_events():
    html = read_page()
    assert 'class="layout"' in html
    assert "min-width:901px" in html
    assert "inventory_created" in html
    assert "已在 T+ 创建存货" in html
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_bom_builder_page.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

CSS 区（`@media(max-width:420px)` 行前）加：

```css
    @media(min-width:901px){.wrap{max-width:1100px}.layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px;align-items:start}.layout>*{margin-bottom:0}}
```

HTML：父件 panel 与子件 panel 外包一层（`<div id="msg">` 之后、`submissionBox` 之前）：

```html
    <div class="layout">
      <section class="panel" id="parentPanel"> ...（原父件 panel 不动）... </section>
      <section class="panel"> ...（原子件 panel 不动）... </section>
    </div>
```

`pollSubmission` 的 `submissionBox.innerHTML` 模板里，`尝试次数` 那行 `</div>` 之后插入：

```js
${(data.events||[]).filter((ev)=>['inventory_created','inventory_reused'].includes(ev.type)).map((ev)=>`<div class="small" style="margin-top:5px;color:var(--good)">${ev.type==='inventory_created'?'已在 T+ 创建存货':'复用 T+ 已有存货'} ${esc((ev.detail||{}).code||'')} ${esc((ev.detail||{}).name||'')}</div>`).join('')}
```

- [ ] **Step 4: 跑前端全部静态测试确认通过**

Run: `python -m pytest tests/test_bom_builder_page.py tests/test_tplus_bom_builder_frontend.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 手工冒烟（本地静态起服）**

Run: 仓库根 `python -m http.server 8080 --directory services/public-web` 后浏览器开 `http://localhost:8080/bom-builder/`
检查（未登录态即可，接口调用会提示登录属预期）：桌面 >901px 两栏、缩窄单列、三分组可折叠、属性默认勾选正确（父件 5 项、虚拟件不勾）。

- [ ] **Step 6: 提交**

```bash
git add services/public-web/bom-builder/index.html tests/test_bom_builder_page.py
git commit -m "feat(bom-builder): 桌面两栏布局 + 提交卡展示存货创建事件"
```

---

### Task 8: 全量回归 + VERSION bump + PR

**Files:**
- Modify: `VERSION`（v2.1.14 → v2.1.15）

- [ ] **Step 1: backend 全量测试**

Run: 仓库根 `python -m pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 2: worker 全量测试**

Run: `cd services/tplus-sync-worker && PYTHONPATH=src python -m pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 3: bump VERSION 并提交**

`VERSION` 内容改为 `v2.1.15`。

```bash
git add VERSION
git commit -m "chore: bump VERSION v2.1.15"
```

- [ ] **Step 4: 推分支开 PR**

```bash
git push origin feat/bom-inventory-create
gh pr create --title "feat(bom-builder): 新建存货完整选项+建议编码查重+T+报错透传+桌面两栏" --body "见 docs/superpowers/specs/2026-07-13-bom-builder-inventory-create-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

### Task 9: 生产写入开关持久修复 + 部署验证（⚠️ 需用户明确授权后才执行）

**Files:** 无仓库文件；操作 `infra/secrets`（sops）与 ECS 生产容器。

**前置：** 用户明确授权动生产。

- [ ] **Step 1: sops 源补键**

devbox（本机）在 infra/secrets 仓库：

```bash
sops set aliecs.enc.env '["TPLUS_BOM_WRITE_ENABLED"]' '"true"'
git add aliecs.enc.env && git commit -m "chore: TPLUS_BOM_WRITE_ENABLED=true 进 sops 基线" && git push
```

（Windows 需 `SOPS_AGE_KEY_FILE` 环境变量，见既有流程。）

- [ ] **Step 2: ECS render + 重建两容器**

```bash
ssh aliecs "cd /root/infra && git pull --ff-only && ./secrets/render.sh && cd /root/AliECS/deploy/ecs && docker compose -f compose.prod.yml up -d --no-build --force-recreate backend-api tplus-write-worker"
```

（compose 服务名以 `compose.prod.yml` 实际为准，执行前 `grep -n 'tplus-write' compose.prod.yml` 确认。）

- [ ] **Step 3: 验证**

```bash
ssh aliecs "docker exec ecs-backend-api-1 printenv TPLUS_BOM_WRITE_ENABLED; docker exec ecs-tplus-write-worker-1 printenv TPLUS_BOM_WRITE_ENABLED"
```

Expected: 两个都输出 `true`。再让用户在页面上提交一单真实 BOM 验收（含新建父件路径），确认不再报"尚未启用"。

- [ ] **Step 4: 记录**

若 render.sh 的产物路径与 `release-meta.env` 的衔接和预期不符（例如 render 只写别的文件），停下来查清 `release-meta.env` 的生成链再动，不要盲改。
