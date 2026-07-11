# BOM Builder 行内录入重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `/bom-builder/` 从「统一搜索+点添加」改为「子件常驻搜索行逐行录入」，父件默认新建态，草稿自动保存，登录修为 SSO。

**Architecture:** 单文件静态页重写（`services/public-web/bom-builder/index.html`，repo 惯例：单 HTML 全内联）。后端接口零改动，沿用 `/v1/tplus/inventories`、`/v1/tplus/inventory-create-options`、bom-drafts 草稿/校验/提交/轮询链路。页面结构测试用 pytest 静态断言（CI 可跑），交互用本地 mock server 手动冒烟 + 上线后真机闭环。

**Tech Stack:** 原生 HTML/CSS/JS（无框架，与 public-web 其他页一致）、pytest（静态断言）、python http.server（本地 mock 冒烟）。

**Spec:** `docs/superpowers/specs/2026-07-11-bom-builder-inline-entry-design.md`

## Global Constraints

- 后端任何文件不改；只改 `services/public-web/bom-builder/index.html`，只新增 `tests/test_bom_builder_page.py`。
- 手机优先：数量输入必须 `inputmode="decimal"`；点击区≥40px；吸底提交条留 `env(safe-area-inset-bottom)`。
- 登录只走 SSO：`/v1/auth/oidc/login?rd=<当前路径>`；禁止出现 `/v1/auth/login` 调用与密码输入框。
- 默认值按**名称匹配**：父件类别「物料清单」、原料类别「原材料」、单位「kg」；名称匹配不到回退现有编码（父件类 `06`、原料类 `01`、单位 `1`）；两者都不在选项里则不预选。
- 合计**不同单位不相加**，按 unit_name 分组显示。
- 草稿 payload 契约（后端 pydantic，勿改字段名）：
  - parent: `{code*, name, specification, unit_name*, unit_code, inventory_class_code, inventory_class_name}`
  - children[]: parent 字段 + `{required_quantity*, warehouse_code, child_bom_version}`
  - options: `{version*, produce_quantity, yield_rate, is_default_bom, warehouse_code, routing_code, manufacture_plant_code}`
- 提交幂等键沿用 `Idempotency-Key: bom-builder-<draftId>`。
- localStorage 键：草稿 `bom_builder_draft_v2`；token 读写沿用 `aliecs_auth_token`/`portal_token`/`admin_token` 三键。
- worktree：`scratchpad\bom-inline`，分支 `feat/bom-builder-inline-entry`（已存在，spec 已提交）。

---

### Task 1: 生产数据核实（默认类别/单位名）

**Files:** 无代码改动；结论记入 Task 3 常量与本文件执行记录。

**Interfaces:**
- Produces: 三个常量的最终取值 —— `DEFAULT_PARENT_CLASS_NAME`（预期「物料清单」）、`DEFAULT_MATERIAL_CLASS_NAME`（预期「原材料」）、`DEFAULT_UNIT_NAME`（预期「kg」），以及是否保留回退编码 `06`/`01`/`1`。

- [ ] **Step 1: 查存货档案中类别与单位的真实名称**

```bash
ssh aliecs 'ls -t /srv/aliecs/data/tplus-exports/ | head -20'
# 找到最新 inventory*.xlsx 与 bom*.xlsx（实际目录以 backend env TPLUS_EXPORT_DIR 为准，
# 可先: ssh aliecs "docker exec aliecs-backend-api-1 env | grep -i tplus"）
ssh aliecs 'docker exec aliecs-backend-api-1 python - <<PY
import glob, pandas as pd
inv = sorted(glob.glob("/app/data/tplus-exports/*inventory*.xlsx"))[-1]
df = pd.read_excel(inv, dtype=str).fillna("")
cls = [c for c in df.columns if "Class" in c or "分类" in c]
unit = [c for c in df.columns if "Unit" in c or "单位" in c]
print("class cols:", cls); print("unit cols:", unit)
print(df[cls[1] if len(cls)>1 else cls[0]].value_counts().head(10))
print(df[unit[1] if len(unit)>1 else unit[0]].value_counts().head(10))
PY'
```

Expected: 类别分布里能看到「物料清单」「原材料」（或它们的真实叫法），单位分布里能看到「kg」（或「千克」等真实叫法）。

- [ ] **Step 2: 查现有 BOM 父件实际用的类别/单位**

```bash
ssh aliecs 'docker exec aliecs-backend-api-1 python - <<PY
import glob, pandas as pd
bom = sorted(glob.glob("/app/data/tplus-exports/*bom*.xlsx"))[-1]
df = pd.read_excel(bom, dtype=str).fillna("")
print(df.columns.tolist()[:30])
# 找父件编码列后与存货档案 join 看类别/单位分布（列名以实际为准）
PY'
```

Expected: 现有 BOM 父件的类别集中在某一类（预期=物料清单），单位集中在 kg。

- [ ] **Step 3: 固化结论**

把 Step1/2 得到的**真实名称字符串**替换进 Task 3 代码顶部三个 `DEFAULT_*_NAME` 常量（若与预期一致则不用改）。若某名称在数据里不存在，保留名称匹配逻辑（匹配不到自动回退编码），并在 PR 描述里注明。

---

### Task 2: 失败的页面结构测试

**Files:**
- Test: `tests/test_bom_builder_page.py`（新建）

**Interfaces:**
- Produces: 对最终页面的结构断言；Task 3 的完成判定 = 本测试全绿。

- [ ] **Step 1: 写测试**

```python
"""bom-builder 页面结构断言：行内录入重构后的关键锚点与禁止项。"""
from __future__ import annotations

from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "services" / "public-web" / "bom-builder" / "index.html"


def read_page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_login_is_sso_only():
    html = read_page()
    assert "oidc/login?rd=" in html
    assert "/v1/auth/login" not in html
    assert "passwordInput" not in html
    assert "authModal" not in html


def test_unified_search_section_removed():
    html = read_page()
    assert 'id="searchScope"' not in html
    assert 'id="searchBtn"' not in html


def test_inline_adder_anchors_present():
    html = read_page()
    for anchor in ("adderInput", "adderResults", "childList", "totalsLine", "submitBtn"):
        assert f'id="{anchor}"' in html, anchor


def test_parent_card_new_mode_anchors_present():
    html = read_page()
    for anchor in (
        "parentCode", "parentName", "parentClassSelect", "parentUnitSelect",
        "parentModeBtn", "parentSearchInput", "parentResults",
    ):
        assert f'id="{anchor}"' in html, anchor


def test_mobile_and_autosave_essentials():
    html = read_page()
    assert 'inputmode="decimal"' in html
    assert "bom_builder_draft_v2" in html
    assert "物料清单" in html
    assert "Idempotency-Key" in html
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_bom_builder_page.py -q`
Expected: FAIL（现页面含 authModal/searchBtn，缺 adderInput 等锚点）。

- [ ] **Step 3: Commit**

```bash
git add tests/test_bom_builder_page.py
git commit -m "test(bom-builder): 行内录入重构页面结构断言（先红）"
```

---

### Task 3: 重写 bom-builder 页面

**Files:**
- Modify: `services/public-web/bom-builder/index.html`（整文件替换）

**Interfaces:**
- Consumes: Task 1 的三个 `DEFAULT_*_NAME` 常量取值；后端契约见 Global Constraints。
- Produces: 最终页面；Task 4/5 直接使用。

- [ ] **Step 1: 用下面完整内容替换 index.html**

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>AliECS · 新建 T+ BOM</title>
  <style>
    :root{--bg:#f5f6f2;--panel:#fff;--text:#20251f;--muted:#687066;--line:#dfe5dc;--primary:#275b45;--danger:#a13e35;--good:#39734e;--warn:#9a6a18;--shadow:0 12px 32px rgba(35,63,48,.08)}
    *{box-sizing:border-box}.hidden{display:none!important}
    body{margin:0;background:var(--bg);color:var(--text);font-family:ui-sans-serif,system-ui}
    .wrap{max-width:760px;margin:auto;padding:16px 14px 120px}
    .topbar,.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:14px 16px;margin-bottom:14px}
    .topbar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
    .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
    h1{margin:0;font-size:22px}h2{margin:0 0 10px;font-size:16px}
    .muted{color:var(--muted)}.small{font-size:13px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .field{display:flex;flex-direction:column;gap:4px;min-width:0}.field.wide{grid-column:span 2}
    label{font-size:12px;color:var(--muted)}
    input,select{border:1px solid var(--line);border-radius:10px;padding:10px 11px;font:inherit;background:#fff;min-width:0;width:100%}
    .btn,button{border:0;border-radius:999px;padding:9px 14px;background:#e6ece6;color:var(--text);font-weight:650;cursor:pointer;text-decoration:none}
    .primary{background:var(--primary);color:#fff}.danger{background:#f4dedb;color:var(--danger)}
    button:disabled{opacity:.55;cursor:not-allowed}
    .linklike{background:none;border:0;color:var(--primary);text-decoration:underline;font-weight:600;padding:6px 0}
    .msg{display:none;border-left:4px solid var(--danger);padding:10px 12px;background:#fff;border-radius:8px;margin-bottom:12px;white-space:pre-wrap}
    .msg.good{border-color:var(--good)}.msg.warn{border-color:var(--warn)}
    details.adv{margin-top:8px}details.adv summary{cursor:pointer;color:var(--muted);font-size:13px;padding:4px 0}
    .child-row{border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-bottom:8px;background:#fbfcfa}
    .child-row.flash{animation:flash 1.2s}
    @keyframes flash{0%,60%{background:#fff3d6}100%{background:#fbfcfa}}
    .child-top{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
    .child-name{flex:1;min-width:0;overflow-wrap:anywhere}
    .qty-line{display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap}
    .qty-line input.qty{width:130px}
    .tag{display:inline-block;padding:3px 8px;border-radius:999px;background:#e8ece7;font-size:12px}
    .tag.custom{background:#fff0d5;color:#795515}
    .adder{position:relative;margin-top:4px}
    .dropdown{position:absolute;left:0;right:0;top:calc(100% + 4px);background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);max-height:46vh;overflow:auto;z-index:6}
    .dd-item{padding:11px 12px;border-bottom:1px solid var(--line);cursor:pointer}
    .dd-item:last-child{border-bottom:0}.dd-item:active{background:#f0f4ef}
    .sticky-bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid var(--line);padding:10px 14px calc(10px + env(safe-area-inset-bottom));display:flex;justify-content:space-between;align-items:center;gap:10px;z-index:5}
    .chosen{padding:10px;border:1px solid var(--line);border-radius:12px;background:#f8faf7}
    .status{display:inline-block;padding:5px 9px;border-radius:999px;background:#e8ece7;font-size:13px}
    .modal{position:fixed;inset:0;background:rgba(18,30,22,.32);display:none;align-items:center;justify-content:center;padding:16px;z-index:10}
    .modal.show{display:flex}
    .modal-panel{width:min(560px,100%);max-height:92vh;overflow:auto;background:#fff;border-radius:16px;padding:16px;border:1px solid var(--line)}
    .hint{padding:10px;border-radius:10px;background:#f2f6f1;color:var(--muted);font-size:13px;margin-bottom:10px}
    @media(max-width:420px){.grid{grid-template-columns:1fr}.field.wide{grid-column:auto}}
  </style>
</head>
<body>
  <main class="wrap">
    <header class="topbar">
      <div><h1>新建 T+ BOM</h1><div class="muted small">逐行搜索原料录入；写入前经服务端校验与人工确认</div></div>
      <nav class="row"><a class="btn" href="/">首页</a><button id="loginBtn" type="button">登录（SSO）</button><button id="logoutBtn" class="hidden" type="button">退出登录</button></nav>
    </header>
    <div id="msg" class="msg"></div>

    <section class="panel" id="parentPanel">
      <div class="row" style="justify-content:space-between"><h2 style="margin:0">父件</h2><button id="parentModeBtn" class="linklike" type="button">改为选用 T+ 已有存货</button></div>
      <div id="parentNewBox">
        <div class="grid" style="margin-top:10px">
          <div class="field"><label for="parentCode">父件编码 *</label><input id="parentCode" maxlength="100" autocomplete="off"/></div>
          <div class="field"><label for="parentName">父件名称 *</label><input id="parentName" maxlength="200" autocomplete="off"/></div>
          <div class="field"><label for="parentClassSelect">所属类别 *</label><select id="parentClassSelect"><option value="">读取 T+ 分类中…</option></select></div>
          <div class="field"><label for="parentUnitSelect">单位 *</label><select id="parentUnitSelect"><option value="">读取单位中…</option></select></div>
        </div>
      </div>
      <div id="parentPickBox" class="hidden" style="margin-top:10px">
        <div class="adder"><input id="parentSearchInput" placeholder="搜 T+ 存货：编码 / 名称 / 规格" autocomplete="off"/><div id="parentResults" class="dropdown hidden"></div></div>
        <div id="parentChosenBox" class="chosen hidden" style="margin-top:8px"></div>
      </div>
      <div class="grid" style="margin-top:10px">
        <div class="field"><label for="versionInput">版本号 *</label><input id="versionInput" value="V1" maxlength="100"/></div>
        <div class="field"><label for="produceQtyInput">生产数量 *</label><input id="produceQtyInput" type="number" inputmode="decimal" min="0.000001" step="any" value="1"/></div>
        <div class="field wide"><label class="row" style="color:var(--text);font-size:14px"><input id="defaultBomInput" type="checkbox" style="width:auto"/> 创建后设为默认 BOM</label></div>
      </div>
      <details class="adv"><summary>高级（成品率 / 规格 / 预入仓库 / 工艺路线 / 车间）</summary>
        <div class="grid" style="margin-top:8px">
          <div class="field"><label for="yieldRateInput">成品率（0～1）</label><input id="yieldRateInput" type="number" inputmode="decimal" min="0.000001" max="1" step="any" value="1"/></div>
          <div class="field"><label for="parentSpec">规格型号</label><input id="parentSpec" maxlength="200"/></div>
          <div class="field"><label for="warehouseInput">预入仓库编码</label><input id="warehouseInput" maxlength="100"/></div>
          <div class="field"><label for="routingInput">工艺路线编码</label><input id="routingInput" maxlength="100"/></div>
          <div class="field"><label for="plantInput">生产车间编码</label><input id="plantInput" maxlength="100"/></div>
        </div>
      </details>
    </section>

    <section class="panel">
      <h2>子件（原料 + 需用数量）</h2>
      <div id="childList"></div>
      <div class="adder"><input id="adderInput" placeholder="搜原料：编码 / 名称 / 规格" autocomplete="off"/><div id="adderResults" class="dropdown hidden"></div></div>
    </section>

    <div id="submissionBox" class="panel hidden"></div>
  </main>

  <div class="sticky-bar"><div><div id="totalsLine" class="small">0 项</div><div id="saveHint" class="small muted"></div></div><button id="submitBtn" class="primary" type="button">校验并写入 T+</button></div>

  <div id="customModal" class="modal"><div class="modal-panel">
    <div class="row" style="justify-content:space-between"><h2 style="margin:0">新增原料存货</h2><button id="closeCustomBtn" type="button">关闭</button></div>
    <div class="hint">该原料将在提交时先创建到 T+ 并审核存货档案（编码不能与已有存货重复）。</div>
    <div class="grid">
      <div class="field"><label for="customCode">存货编码 *</label><input id="customCode" maxlength="100"/></div>
      <div class="field"><label for="customName">存货名称 *</label><input id="customName" maxlength="200"/></div>
      <div class="field wide"><label for="customSpec">规格型号</label><input id="customSpec" maxlength="200"/></div>
      <div class="field"><label for="customClassSelect">存货分类 *</label><select id="customClassSelect"><option value="">读取中…</option></select></div>
      <div class="field"><label for="customUnitSelect">计量单位 *</label><select id="customUnitSelect"><option value="">读取中…</option></select></div>
    </div>
    <div class="row" style="justify-content:flex-end;margin-top:14px"><button id="addCustomBtn" class="primary" type="button">加入子件</button></div>
  </div></div>

  <script>
    const API_BASE=location.port==='8080'?'http://localhost:8000':'/api';
    const AUTH_KEYS=['aliecs_auth_token','portal_token','admin_token'];
    const LS_KEY='bom_builder_draft_v2';
    const DEFAULT_PARENT_CLASS_NAME='物料清单';
    const DEFAULT_MATERIAL_CLASS_NAME='原材料';
    const DEFAULT_UNIT_NAME='kg';
    const FALLBACK_PARENT_CLASS_CODE='06',FALLBACK_MATERIAL_CLASS_CODE='01',FALLBACK_UNIT_CODE='1';
    const $=(id)=>document.getElementById(id);
    const state={parentMode:'new',parentPicked:null,children:[],draftId:null,submissionId:null,options:null};
    const token=()=>AUTH_KEYS.map((k)=>localStorage.getItem(k)||'').find(Boolean)||'';
    const clearToken=()=>AUTH_KEYS.forEach((k)=>localStorage.removeItem(k));
    const esc=(v)=>String(v??'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const ssoLogin=()=>{location.href=`${API_BASE}/v1/auth/oidc/login?rd=${encodeURIComponent(location.pathname+location.search)}`;};
    const fmtQty=(n)=>Number(n).toLocaleString('zh-CN',{maximumFractionDigits:4});
    function message(text,type='error'){const box=$('msg');box.textContent=text;box.className=`msg ${type==='good'?'good':type==='warn'?'warn':''}`;box.style.display=text?'block':'none';}
    function syncAuth(){$('loginBtn').classList.toggle('hidden',!!token());$('logoutBtn').classList.toggle('hidden',!token());}
    async function api(path,opt={}){const headers=Object.assign({'Content-Type':'application/json'},opt.headers||{});if(token())headers.Authorization=`Bearer ${token()}`;const response=await fetch(`${API_BASE}${path}`,{...opt,headers});const text=await response.text();let data={};if(text){try{data=JSON.parse(text)}catch{data={raw:text}}}if(!response.ok){if(response.status===401){clearToken();syncAuth();}throw new Error(data.detail||`HTTP ${response.status}`)}return data;}
    function debounce(fn,ms){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms)};}

    // ---------- 选项（类别/单位）：名称匹配预选，匹配不到回退编码 ----------
    function pickOption(select,preferName,fallbackCode){const byName=[...select.options].find((o)=>o.dataset.name===preferName);if(byName){select.value=byName.value;return}if([...select.options].some((o)=>o.value===fallbackCode))select.value=fallbackCode;}
    async function ensureOptions(){if(state.options)return state.options;const data=await api('/v1/tplus/inventory-create-options');state.options=data;for(const select of [$('parentClassSelect'),$('customClassSelect')])select.innerHTML=(data.classes||[]).map((x)=>`<option value="${esc(x.code)}" data-name="${esc(x.name)}">${esc(x.code)} · ${esc(x.name)}</option>`).join('');
      for(const select of [$('parentUnitSelect'),$('customUnitSelect')])select.innerHTML=(data.units||[]).map((x)=>`<option value="${esc(x.code)}" data-name="${esc(x.name)}">${esc(x.name)}</option>`).join('');
      pickOption($('parentClassSelect'),DEFAULT_PARENT_CLASS_NAME,FALLBACK_PARENT_CLASS_CODE);
      pickOption($('customClassSelect'),DEFAULT_MATERIAL_CLASS_NAME,FALLBACK_MATERIAL_CLASS_CODE);
      pickOption($('parentUnitSelect'),DEFAULT_UNIT_NAME,FALLBACK_UNIT_CODE);
      pickOption($('customUnitSelect'),DEFAULT_UNIT_NAME,FALLBACK_UNIT_CODE);
      return data;}

    // ---------- 父件 ----------
    function parentItem(){if(state.parentMode==='pick')return state.parentPicked;
      const classOption=$('parentClassSelect').selectedOptions[0],unitOption=$('parentUnitSelect').selectedOptions[0];
      return{source:'custom',code:$('parentCode').value.trim(),name:$('parentName').value.trim(),specification:$('parentSpec').value.trim(),inventory_class_code:classOption?.value||'',inventory_class_name:classOption?.dataset.name||'',unit_code:unitOption?.value||'',unit_name:unitOption?.dataset.name||''};}
    function renderParentChosen(){const p=state.parentPicked;$('parentChosenBox').classList.toggle('hidden',!p);if(p)$('parentChosenBox').innerHTML=`<span class="tag">T+ 已有</span> <b>${esc(p.code)} · ${esc(p.name)}</b><div class="muted small">${esc(p.specification||'无规格')} · ${esc(p.unit_name)}</div><button class="linklike" id="unpickParentBtn" type="button">撤销，改回新建父件</button>`;const btn=$('unpickParentBtn');if(btn)btn.onclick=()=>{state.parentPicked=null;setParentMode('new');markDirty();};}
    function setParentMode(mode){state.parentMode=mode;$('parentNewBox').classList.toggle('hidden',mode!=='new');$('parentPickBox').classList.toggle('hidden',mode!=='pick');$('parentModeBtn').textContent=mode==='new'?'改为选用 T+ 已有存货':'改回新建父件';renderParentChosen();}

    // ---------- 搜索下拉（父件/子件共用） ----------
    function renderDropdown(box,items,keyword,onPick,{allowCreate}={}){
      const rows=items.map((p,i)=>`<div class="dd-item" data-i="${i}"><b>${esc(p.name)}</b> <span class="muted small">${esc(p.specification||'')}</span><div class="muted small">${esc(p.code)} · ${esc(p.unit_name)}${p.available_quantity===undefined?'':` · 可用 ${fmtQty(p.available_quantity)}`}</div></div>`).join('');
      const create=allowCreate?`<div class="dd-item" data-create="1" style="color:var(--primary)">＋ 新建“${esc(keyword)}”</div>`:'';
      box.innerHTML=(rows||(allowCreate?'':'<div class="dd-item muted">没有匹配存货</div>'))+create;
      box.classList.remove('hidden');
      box.querySelectorAll('[data-i]').forEach((el)=>el.onclick=()=>{box.classList.add('hidden');onPick(items[Number(el.dataset.i)]);});
      const createEl=box.querySelector('[data-create]');
      if(createEl)createEl.onclick=()=>{box.classList.add('hidden');openCustomModal(keyword);};}
    function attachSearch(input,box,scope,onPick,{allowCreate=false}={}){
      const run=debounce(async()=>{const q=input.value.trim();if(!q){box.classList.add('hidden');return}
        if(!token()){message('请先登录（SSO）。');return}
        try{const data=await api(`/v1/tplus/inventories?q=${encodeURIComponent(q)}&limit=20&scope=${scope}`);if(input.value.trim()!==q)return;renderDropdown(box,data.items||[],q,onPick,{allowCreate});}
        catch(e){box.innerHTML=`<div class="dd-item" style="color:var(--danger)">搜索失败：${esc(e.message)}（改动关键词重试）</div>`;box.classList.remove('hidden');}},250);
      input.addEventListener('input',run);
      input.addEventListener('focus',()=>{if(input.value.trim())run();});}

    // ---------- 子件 ----------
    function findChild(code){return state.children.findIndex((c)=>c.code===code);}
    function addChild(item){
      const parent=parentItem();
      if(parent&&parent.code&&parent.code===item.code){message('父件不能作为自己的子件。');return}
      const existing=findChild(item.code);
      if(existing>=0){renderChildren();const row=$('childList').children[existing];row.classList.add('flash');row.scrollIntoView({block:'center'});row.querySelector('input.qty').focus();setTimeout(()=>row.classList.remove('flash'),1300);return}
      state.children.push({...item,required_quantity:'',warehouse_code:'',child_bom_version:''});
      markDirty();renderChildren();
      const row=$('childList').lastElementChild;row.scrollIntoView({block:'center'});row.querySelector('input.qty').focus();}
    function renderChildren(){
      $('childList').innerHTML=state.children.map((c,i)=>`<div class="child-row"><div class="child-top"><div class="child-name">${c.source==='custom'?'<span class="tag custom">新建 T+</span> ':''}<b>${esc(c.name)}</b><div class="muted small">${esc(c.code)}${c.specification?` · ${esc(c.specification)}`:''}${c.available_quantity===undefined?'':` · 可用 ${fmtQty(c.available_quantity)}`}</div></div><button class="danger" data-remove="${i}" type="button">删除</button></div><div class="qty-line"><label style="font-size:14px;color:var(--text)">需用数量</label><input class="qty" data-child="${i}" data-field="required_quantity" type="number" inputmode="decimal" min="0.000001" step="any" value="${esc(c.required_quantity)}" enterkeyhint="next"/><span class="muted small">${esc(c.unit_name)}</span></div><details class="adv"><summary>更多（预出仓库 / 子 BOM 版本）</summary><div class="qty-line"><input data-child="${i}" data-field="warehouse_code" placeholder="预出仓库编码" value="${esc(c.warehouse_code)}" style="width:150px"/><input data-child="${i}" data-field="child_bom_version" placeholder="子 BOM 版本" value="${esc(c.child_bom_version)}" style="width:130px"/></div></details></div>`).join('');
      $('childList').querySelectorAll('[data-field]').forEach((el)=>el.oninput=()=>{state.children[Number(el.dataset.child)][el.dataset.field]=el.value;markDirty();renderTotals();});
      $('childList').querySelectorAll('[data-remove]').forEach((el)=>el.onclick=()=>{state.children.splice(Number(el.dataset.remove),1);markDirty();renderChildren();});
      $('childList').querySelectorAll('input.qty').forEach((el)=>el.onkeydown=(e)=>{if(e.key==='Enter'){e.preventDefault();$('adderInput').focus();}});
      renderTotals();}
    function renderTotals(){
      const groups=new Map();
      for(const c of state.children){const qty=Number(c.required_quantity);if(!Number.isFinite(qty)||qty<=0)continue;const unit=c.unit_name||'?';groups.set(unit,(groups.get(unit)||0)+qty);}
      const parts=[...groups.entries()].map(([unit,sum])=>`${fmtQty(sum)} ${esc(unit)}`);
      $('totalsLine').innerHTML=`${state.children.length} 项${parts.length?` · 合计 ${parts.join(' ＋ ')}`:''}`;}

    // ---------- 新增自定义原料 ----------
    async function openCustomModal(prefillName){
      if(!token()){message('请先登录（SSO）。');return}
      try{await ensureOptions()}catch(e){message(e.message);return}
      $('customCode').value='';$('customName').value=prefillName||'';$('customSpec').value='';
      pickOption($('customClassSelect'),DEFAULT_MATERIAL_CLASS_NAME,FALLBACK_MATERIAL_CLASS_CODE);
      pickOption($('customUnitSelect'),DEFAULT_UNIT_NAME,FALLBACK_UNIT_CODE);
      $('customModal').classList.add('show');$('customCode').focus();}
    function addCustom(){
      const classOption=$('customClassSelect').selectedOptions[0],unitOption=$('customUnitSelect').selectedOptions[0];
      const item={source:'custom',code:$('customCode').value.trim(),name:$('customName').value.trim(),specification:$('customSpec').value.trim(),inventory_class_code:classOption?.value||'',inventory_class_name:classOption?.dataset.name||'',unit_code:unitOption?.value||'',unit_name:unitOption?.dataset.name||''};
      for(const [key,label] of [['code','存货编码'],['name','存货名称'],['inventory_class_code','存货分类'],['unit_code','计量单位']]){if(!item[key]){message(`新增原料的${label}不能为空。`);return}}
      $('customModal').classList.remove('show');$('adderInput').value='';addChild(item);}

    // ---------- 自动保存 ----------
    function snapshot(){return{parentMode:state.parentMode,parentPicked:state.parentPicked,children:state.children,draftId:state.draftId,form:{parentCode:$('parentCode').value,parentName:$('parentName').value,parentClass:$('parentClassSelect').value,parentUnit:$('parentUnitSelect').value,parentSpec:$('parentSpec').value,version:$('versionInput').value,produceQty:$('produceQtyInput').value,yieldRate:$('yieldRateInput').value,warehouse:$('warehouseInput').value,routing:$('routingInput').value,plant:$('plantInput').value,defaultBom:$('defaultBomInput').checked}};}
    function payload(){const form=snapshot().form;return{parent:parentItem(),children:state.children.map((c)=>({...c,required_quantity:String(c.required_quantity)})),options:{version:form.version.trim(),produce_quantity:form.produceQty,yield_rate:form.yieldRate,is_default_bom:form.defaultBom,warehouse_code:form.warehouse.trim(),routing_code:form.routing.trim(),manufacture_plant_code:form.plant.trim()}};}
    const serverSave=debounce(async()=>{if(!token())return;try{const body=JSON.stringify(payload());const data=state.draftId?await api(`/v1/tplus/bom-drafts/${state.draftId}`,{method:'PATCH',body}):await api('/v1/tplus/bom-drafts',{method:'POST',body});state.draftId=data.id;localStorage.setItem(LS_KEY,JSON.stringify(snapshot()));$('saveHint').textContent=`草稿 #${state.draftId} 已自动保存`;}catch{ $('saveHint').textContent='服务端草稿保存失败（本地已暂存）';}},2000);
    function markDirty(){localStorage.setItem(LS_KEY,JSON.stringify(snapshot()));$('saveHint').textContent=token()?'保存中…':'未登录：仅本地暂存';if(token())serverSave();}
    function restore(){const raw=localStorage.getItem(LS_KEY);if(!raw)return;let saved;try{saved=JSON.parse(raw)}catch{return}
      const hasContent=(saved.children&&saved.children.length)||(saved.form&&(saved.form.parentCode||saved.form.parentName));
      if(!hasContent)return;
      if(!confirm('检测到上次未提交的配方，恢复继续录入吗？（取消则丢弃）')){localStorage.removeItem(LS_KEY);return}
      state.children=saved.children||[];state.draftId=saved.draftId||null;state.parentPicked=saved.parentPicked||null;
      const form=saved.form||{};$('parentCode').value=form.parentCode||'';$('parentName').value=form.parentName||'';$('parentSpec').value=form.parentSpec||'';$('versionInput').value=form.version||'V1';$('produceQtyInput').value=form.produceQty||'1';$('yieldRateInput').value=form.yieldRate||'1';$('warehouseInput').value=form.warehouse||'';$('routingInput').value=form.routing||'';$('plantInput').value=form.plant||'';$('defaultBomInput').checked=!!form.defaultBom;
      if(form.parentClass)state.pendingParentClass=form.parentClass;if(form.parentUnit)state.pendingParentUnit=form.parentUnit;
      setParentMode(saved.parentMode==='pick'&&state.parentPicked?'pick':'new');renderChildren();}

    // ---------- 校验并写入 ----------
    function localCheck(){const p=parentItem();
      if(!p||!p.code)throw new Error('请填写父件编码。');
      if(state.parentMode==='new'){if(!p.name)throw new Error('请填写父件名称。');if(!p.inventory_class_code)throw new Error('请选择父件所属类别。');if(!p.unit_code)throw new Error('请选择父件单位。');}
      if(!$('versionInput').value.trim())throw new Error('请填写版本号。');
      if(!state.children.length)throw new Error('请至少添加一个子件。');
      const bad=state.children.find((c)=>{const qty=Number(c.required_quantity);return !Number.isFinite(qty)||qty<=0});
      if(bad)throw new Error(`子件「${bad.name}」的需用数量未填或不合法。`);
      if(state.children.some((c)=>c.code===p.code))throw new Error('父件不能同时出现在子件里。');}
    async function saveNow(){const body=JSON.stringify(payload());const data=state.draftId?await api(`/v1/tplus/bom-drafts/${state.draftId}`,{method:'PATCH',body}):await api('/v1/tplus/bom-drafts',{method:'POST',body});state.draftId=data.id;localStorage.setItem(LS_KEY,JSON.stringify(snapshot()));return data;}
    async function submit(){message('');if(!token()){message('请先登录（SSO）。');return}
      $('submitBtn').disabled=true;
      try{localCheck();await saveNow();
        const check=await api(`/v1/tplus/bom-drafts/${state.draftId}/validate`,{method:'POST'});
        if(!check.valid)throw new Error(check.errors.join('\n'));
        const p=parentItem();const customCount=[p,...state.children].filter((x)=>x&&x.source==='custom').length;
        const extra=customCount?`\n其中 ${customCount} 个存货会先在 T+ 创建。`:'';
        if(!confirm(`即将把父件 ${p.code}（${p.name}）、版本 ${$('versionInput').value.trim()}、${state.children.length} 个子件写入畅捷通 T+。${extra}\n\n确认继续吗？`))return;
        const data=await api(`/v1/tplus/bom-drafts/${state.draftId}/submit`,{method:'POST',body:JSON.stringify({confirmed:true}),headers:{'Idempotency-Key':`bom-builder-${state.draftId}`}});
        state.submissionId=data.id;$('submissionBox').classList.remove('hidden');
        message('已进入写入队列，请等待 T+ 返回并完成写后验证。','warn');
        await pollSubmission();}
      catch(e){message(e.message)}
      finally{$('submitBtn').disabled=false;}}
    async function pollSubmission(){if(!state.submissionId)return;const data=await api(`/v1/tplus/bom-submissions/${state.submissionId}`);
      const terminal=['success','failed','needs_review'].includes(data.status);
      const labels={pending:'等待处理',processing:'正在写入 T+',success:'创建并验证成功',failed:'创建失败',needs_review:'需要人工复核'};
      $('submissionBox').innerHTML=`<span class="status">${esc(labels[data.status]||data.status)}</span> <b>提交 #${data.id}</b><div class="muted small" style="margin-top:7px">T+ BOM ID：${esc(data.result_bom_id||'-')} · 尝试次数：${esc(data.attempts)}</div>${data.error&&data.error.message?`<div style="color:var(--danger);margin-top:7px">${esc(data.error.message)}</div>`:''}${data.status==='success'?'<div style="margin-top:10px"><button id="againBtn" class="btn" type="button">再录一个配方</button></div>':''}`;
      const again=$('againBtn');if(again)again.onclick=()=>{localStorage.removeItem(LS_KEY);location.reload();};
      if(!terminal)setTimeout(()=>pollSubmission().catch((e)=>message(e.message)),2000);
      else if(data.status==='success'){localStorage.removeItem(LS_KEY);message('T+ BOM 创建成功，并已完成写后查询验证。','good');}
      else message('提交已停止自动处理，请根据状态人工复核。','warn');}

    // ---------- 装配 ----------
    document.addEventListener('DOMContentLoaded',()=>{
      syncAuth();restore();renderChildren();
      $('loginBtn').onclick=ssoLogin;
      $('logoutBtn').onclick=()=>{clearToken();syncAuth();message('已退出登录。')};
      $('parentModeBtn').onclick=()=>{setParentMode(state.parentMode==='new'?'pick':'new');markDirty();};
      attachSearch($('adderInput'),$('adderResults'),'material',(item)=>{$('adderInput').value='';$('adderResults').classList.add('hidden');addChild(item);},{allowCreate:true});
      attachSearch($('parentSearchInput'),$('parentResults'),'all',(item)=>{state.parentPicked={...item};$('parentSearchInput').value='';renderParentChosen();markDirty();});
      document.addEventListener('click',(e)=>{if(!e.target.closest('.adder'))document.querySelectorAll('.dropdown').forEach((d)=>d.classList.add('hidden'));});
      $('closeCustomBtn').onclick=()=>$('customModal').classList.remove('show');
      $('customModal').onclick=(e)=>{if(e.target===$('customModal'))$('customModal').classList.remove('show')};
      $('addCustomBtn').onclick=addCustom;
      $('submitBtn').onclick=submit;
      document.querySelectorAll('#parentCode,#parentName,#parentSpec,#versionInput,#produceQtyInput,#yieldRateInput,#warehouseInput,#routingInput,#plantInput,#defaultBomInput,#parentClassSelect,#parentUnitSelect').forEach((el)=>{el.addEventListener('input',markDirty);el.addEventListener('change',markDirty);});
      if(token())ensureOptions().then(()=>{if(state.pendingParentClass)$('parentClassSelect').value=state.pendingParentClass;if(state.pendingParentUnit)$('parentUnitSelect').value=state.pendingParentUnit;}).catch((e)=>message(`读取 T+ 分类/单位失败：${e.message}（父件类别与单位暂不可选，可稍后刷新重试）`,'warn'));
      else message('请先点右上角「登录（SSO）」再开始录入。','warn');
    });
  </script>
</body>
</html>
```

- [ ] **Step 2: 跑结构测试**

Run: `python -m pytest tests/test_bom_builder_page.py -q`
Expected: 5 passed。

- [ ] **Step 3: Commit**

```bash
git add services/public-web/bom-builder/index.html
git commit -m "feat(bom-builder): 行内录入重构（常驻搜索行+父件新建态+自动保存+SSO 登录）"
```

---

### Task 4: 本地 mock 冒烟

**Files:**
- Create（仅 scratchpad，不入库）: `<scratchpad>/bom-mock/serve.py`

**Interfaces:**
- Consumes: Task 3 页面（`localhost:8080` 时 API_BASE 指向 `localhost:8000`）。

- [ ] **Step 1: 写 mock 服务**

```python
"""bom-builder 本地冒烟：8080 托管静态页，8000 提供假 API。"""
import json, re, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from functools import partial
from http.server import SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ITEMS = [
    {"code": "0101", "name": "白砂糖", "specification": "25kg/袋", "unit_name": "kg", "unit_code": "1",
     "inventory_class_code": "01", "inventory_class_name": "原材料", "source": "tplus", "available_quantity": 1200.0},
    {"code": "0102", "name": "面粉", "specification": "高筋", "unit_name": "kg", "unit_code": "1",
     "inventory_class_code": "01", "inventory_class_name": "原材料", "source": "tplus", "available_quantity": 800.0},
    {"code": "0103", "name": "鸡蛋", "specification": "", "unit_name": "个", "unit_code": "2",
     "inventory_class_code": "01", "inventory_class_name": "原材料", "source": "tplus", "available_quantity": 3000.0},
]
STATE = {"draft": 0, "polls": 0}

class Api(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.end_headers()
        self.wfile.write(body)
    def do_OPTIONS(self):
        self._send({})
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/v1/auth/me":
            return self._send({"username": "dev", "roles": ["admin"], "permissions": ["admin.access"]})
        if url.path == "/v1/tplus/inventories":
            q = (parse_qs(url.query).get("q", [""])[0]).lower()
            hits = [i for i in ITEMS if q in i["code"].lower() or q in i["name"].lower()]
            return self._send({"items": hits, "total": len(hits), "source_file": "mock.xlsx"})
        if url.path == "/v1/tplus/inventory-create-options":
            return self._send({"classes": [{"code": "01", "name": "原材料"}, {"code": "06", "name": "物料清单"}],
                               "units": [{"code": "1", "name": "kg"}, {"code": "2", "name": "个"}]})
        if re.fullmatch(r"/v1/tplus/bom-submissions/9", url.path):
            STATE["polls"] += 1
            status = "processing" if STATE["polls"] < 3 else "success"
            return self._send({"id": 9, "status": status, "result_bom_id": "T999" if status == "success" else "",
                               "attempts": 1, "error": {}})
        self._send({"detail": "not found"}, 404)
    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/v1/tplus/bom-drafts":
            STATE["draft"] += 1
            return self._send({"id": STATE["draft"]}, 201)
        if re.fullmatch(r"/v1/tplus/bom-drafts/\d+/validate", url.path):
            return self._send({"valid": True, "errors": []})
        if re.fullmatch(r"/v1/tplus/bom-drafts/\d+/submit", url.path):
            STATE["polls"] = 0
            return self._send({"id": 9}, 202)
        self._send({"detail": "not found"}, 404)
    def do_PATCH(self):
        if re.fullmatch(r"/v1/tplus/bom-drafts/\d+", urlparse(self.path).path):
            return self._send({"id": STATE["draft"] or 1})
        self._send({"detail": "not found"}, 404)

def main():
    static = partial(SimpleHTTPRequestHandler, directory=r"<worktree>/services/public-web")
    threading.Thread(target=HTTPServer(("127.0.0.1", 8080), static).serve_forever, daemon=True).start()
    print("static :8080  api :8000  →  http://localhost:8080/bom-builder/")
    HTTPServer(("127.0.0.1", 8000), Api).serve_forever()

if __name__ == "__main__":
    main()
```

（`<worktree>` 替换为实际 worktree 绝对路径。）

- [ ] **Step 2: 跑冒烟清单（浏览器手动，含手机宽度模拟）**

Run: `python <scratchpad>/bom-mock/serve.py`，浏览器开 `http://localhost:8080/bom-builder/`，先在 devtools 执行 `localStorage.setItem('aliecs_auth_token','dev')` 后刷新。逐项验证：

1. 父件卡默认新建态；类别预选「物料清单」、单位预选「kg」。
2. 子件搜索行输「糖」→ 250ms 内出下拉 → 点「白砂糖」→ 行落成已选、焦点在数量框、下拉关闭、搜索框清空。
3. 连续加「面粉」「鸡蛋」；数量填 12.5 / 30 / 3 → 吸底条显示 `3 项 · 合计 42.5 kg ＋ 3 个`。
4. 再搜「白砂糖」点选 → 不重复添加，原行闪烁并聚焦数量。
5. 搜「黄油」（无结果）→ 点「＋ 新建“黄油”」→ 弹窗名称预填、类别预选原材料、单位 kg → 加入后带「新建 T+」标签。
6. 改任意内容 → 吸底条 2 秒内出现「草稿 #N 已自动保存」。
7. 刷新页面 → 提示恢复 → 内容完整（含数量、父件表单）。
8. 「改为选用 T+ 已有存货」→ 搜索选中 → 展示已选卡；撤销回新建态。
9. 点「校验并写入 T+」→ confirm 文案含父件与子件数 → 确认后状态走 等待/写入中→成功，出现「再录一个配方」。
10. 手机宽度（375px）模拟：无横向滚动、吸底条不遮内容、数量键盘为数字。

Expected: 全部通过；发现问题当场修复。

- [ ] **Step 3: 修补后重跑结构测试并提交（若有修补）**

```bash
python -m pytest tests/test_bom_builder_page.py -q
git add services/public-web/bom-builder/index.html
git commit -m "fix(bom-builder): 冒烟修补"
```

---

### Task 5: 全量测试、PR 与上线验证

**Files:** 无新改动。

- [ ] **Step 1: 全量 pytest**

Run: `python -m pytest tests -q`
Expected: 全绿（基线 557+5 passed, 2 skipped）。

- [ ] **Step 2: push + PR**

```bash
git push -u origin feat/bom-builder-inline-entry
gh pr create --title "feat(bom-builder): 行内录入重构（常驻搜索行+父件新建态+自动保存+SSO）" --body "见 docs/superpowers/specs/2026-07-11-bom-builder-inline-entry-design.md；后端零改动；含 Task1 默认值核实结论。"
```

Expected: CI（validate/migration-dry-run）全绿。

- [ ] **Step 3: 合并部署后真机验证（用户参与）**

1. 手机开 `/bom-builder/`，登录（SSO）→ 回跳本页。
2. 新建父件（默认类别/单位正确）→ 常驻行连加 3 个原料（含一次新建自定义）→ 合计正确 → 提交 → 轮询到 success → T+ 中确认 BOM 存在。
3. 录一半杀进程重进 → 恢复提示且内容完整。

---

## Self-Review 记录

- Spec 覆盖：三块结构(T3)、父件新建态+名称匹配预选(T1/T3)、常驻搜索行全部行为(T3)、按单位分组合计(T3 renderTotals)、自动保存两层+恢复(T3)、SSO(T3)、错误处理(attachSearch 行内报错/401 清 token)、非目标未越界（后端零改动）。
- 占位符：无 TBD/TODO；mock `<worktree>` 为执行时替换的路径参数，Step 内已注明。
- 类型/命名一致性：`parentItem()/addChild()/markDirty()/ensureOptions()` 各任务引用一致；payload 字段与后端 pydantic 逐一核对过。
