# 三页面 UI 优化：health 版本巡检入口化 / console 美化 / admin 分区收纳

日期：2026-07-19　状态：已确认

## 背景

首页显示优化已完成（分组卡片行 + 米色设计系统）。三个页面需要跟进：

1. `/health/` 的「版本巡检」区块把逐设备版本明细表全量渲染在页内，信息过载。
2. `/console/`（infra 仓 `console/aliecs/index.html`）是无样式裸链接列表，与站点风格脱节。
3. `/admin/`（`services/admin-ui/index.html`）配色已统一，但六大区块全量平铺，页面过长。

## 决策（用户已确认）

- 版本巡检 → **独立明细页**（同备份区块「查看全部备份」模式）。
- admin → **分区收纳**（默认折叠 + 锚点导航）。
- console → 首页同款设计系统重做。

## 方案

### 1. health 版本巡检入口化（AliECS）

- `services/public-web/health/index.html`：保留「版本巡检」3 张汇总指标卡，移除页内逐设备明细表渲染，加「查看版本明细」主按钮跳 `/health/versions/`。
- 新增 `services/public-web/health/versions/index.html`：同设计系统、同 SSO 管理员门禁，调 `/v1/ops/versions` 渲染逐设备版本表（沿用状态徽章语义：✅ 最新 / 🔴 落后 / 📌 锁定 / ⚠️ 未登记 / ⚪ 一致 / 🟠 不一致 / ⛔ 停滞），带刷新按钮与返回 health 入口。
- `tests/test_version_health_page.py`：断言拆两文件——health 页有汇总与 `/health/versions/` 入口；versions 页有渲染函数、API 路径与状态徽章。

### 2. console 美化（infra）

- 重写 `infra/console/aliecs/index.html`：米色背景、卡片行 + emoji + 圆角阴影 + hover，按设备分组（webdock1 / webdock2 / devbox / aliecs），行内小灰字说明与延迟提示保留。
- **红线**：`@@WEBDOCK1_BROWSER_VNC_PASSWORD@@` / `@@WEBDOCK2_BROWSER_VNC_PASSWORD@@` 占位符与所有链接参数逐字保留（aliecs render.sh sed 注入依赖）；认证机制注释保留；纯静态无 JS。
- 生效：push 全部 remote 后 aliecs 重跑 render.sh（部署动作另行确认）。

### 3. admin 分区收纳（AliECS）

- 用户 / 小程序申请 / 角色权限 / 功能入口 / 系统配置 5 区块统一改 `<details class="card">` 折叠（审计日志已是该模式）：默认收起，summary 保留标题 + 计数徽章。
- 展开状态存 localStorage，回访记住。
- 登录态下方加锚点导航 chips：点击展开对应区块并平滑滚动。
- 所有元素 ID 与 JS 逻辑不动，仅包折叠结构；summary hover 与展开箭头微调。

## 验证

- `python -m unittest discover -s tests`
- 两页内联 JS 语法检查（node --check 提取脚本）
- 核心入口 smoke：登录态下点击能触发网络请求；console 页浏览器目测。

## 回退

三个文件改动独立，逐文件 revert 即可；versions 新页删除不影响 health 主页（入口按钮 404 而已）。
