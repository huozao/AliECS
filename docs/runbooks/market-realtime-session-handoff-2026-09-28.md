# 市场实时页 session 交接（2026-09-28）

本文记录 2026-09-28 本 session 对 `market.hydwang.xyz/market/realtime/` 的修改、生产热更新和验证边界。它是时间点交接材料，不替代源码、生产容器或当前部署记录。

## 当前目标与范围

本次只处理实时页。`/market/today/`、`/market/history/` 和 `/market/` 旧观察台没有按本次布局任务改动。实时页仍是只读页面，不能下单或撤单。

实时页应保持原有国内合约窗口设计：每个合约一个独立窗口，窗口内显示成交价、秒内高低、真实成交曲线、I 价格带上下边缘、I 价格带中心、买卖挂单和源时刻。窗口选择为最近 5、10、15 分钟；选择值必须传给实时 API，减少初始加载量。通用对冲候选排序位于所有国内合约窗口之后，作为独立排序模块，不再在每个合约窗口内重复显示候选列表。

国际行情层和国内行情层仍在页面上方；国际显示美元/克人民币换算、XAU/FX 年龄和更新时间。其本 session 的本地账号登录改造仍有效：市场页面使用 `/api/v1/auth/login`，后端行情 API 继续要求 Bearer Token 与 `market.read`。

## 代码正本与部署关系

AliECS 是页面/API 源码仓：

```text
/home/ishelwsl/src/AliECS-WebDock/AliECS
```

相关入口：

```text
services/public-web/market/realtime/index.html
services/public-web/market/realtime.js
services/public-web/market/market.css
services/public-web/market/page-common.js
```

infra 仓只负责 txecs 的 nginx、域名和服务编排，不是实时页 HTML/JS 的正本：

```text
/home/ishelwsl/src/AliECS-WebDock/infra
```

生产页面当前由 txecs 容器 `business-cn-public-web-1` 提供，文件位于容器内 `/usr/share/nginx/html/market/`。本次页面热更新直接复制了三个实时页静态文件；没有把其他会话的工作区脏文件带入部署。

生产 backend 当前来自 txecs release `c316eaf9ec6108a45c2157791bbb9e3343df3557`。该生产版本已经支持 `window_minutes=5|10|15`，并通过 review source 返回实时数据。当前 WSL AliECS 分支与该生产 release 并非同一份完整源码；新 session 不得因为本地 API 文件较旧就直接重建或覆盖生产 backend。

## 本 session 的提交

AliECS：

```text
1acad22 Highlight stale international quote ages
44e8497 Compact realtime international quote display
87b01f8 Allow local password login for market pages
148087a Restore realtime contract windows and selectors
```

`d2aa89c Arrange realtime contract price-band cards` 是中途采用简化卡片布局的提交，随后由 `148087a` 恢复为原有实时合约窗口结构。新 session 评估布局时应以 `148087a` 和生产回读为准，不把 `d2aa89c` 当作最终视觉基线。

infra：

```text
d0046a8 Use backend auth for market dashboard access
```

该提交移除了市场域名 nginx 层的 Authelia `auth_request`，但 backend Bearer Token/RBAC 仍保留。其他统一登录入口没有改。

## 实时页当前实现的关键细节

`realtime/index.html` 当前包含：

- `window-minutes` 选择器：5、10、15 分钟，默认 5 分钟；
- 国际/国内/重叠/策略四个状态卡；
- `#contracts.grid.realtime-grid` 国内合约窗口区域；
- `#hedge-candidates` 位于 `#contracts` 之后；
- 页面继续加载 Lightweight Charts，但实时图表创建选项设置 `attributionLogo:false`。

`realtime.js` 当前负责：

- 请求 `/api/v1/market/realtime?window_minutes=<5|10|15>`；
- 窗口切换时清理游标和已有数据，重新读取；
- 每个合约创建 `market-card realtime-contract-card`；
- 真实成交线为白色；
- I 价格带上/下边缘为蓝色系；
- I 价格带中心为黄色虚线；
- 右侧显示上方卖挂单、价格带上边缘、真实成交价、价格带中心、价格带下边缘、下方买挂单；
- 不在国内合约窗口中再渲染对冲候选列表；候选只在页面下方统一显示。

后续修复：国内新鲜度由每个精简报价的 `source_time` 计算并显示新鲜合约数；
精简接口不带 `sources.domestic`，不能以其缺失判定国内断线。页面首次先读 `/latest`
显示合约头与价格标签，再读取整窗曲线；后续
用 Review API 的 `next_since` 增量读取；不传 `source_revision`，因为它随每次发布改变，
传入会强制整窗重取。run_id 切换、窗口切换和页面重新显示时重新读取整窗。
页面上的行情重叠状态只说明报价新鲜，不宣称账户执行已获准。验证入口：
`node --test tests/realtime-page.test.cjs`，再以浏览器 Network 核对首屏整窗和后续 `since` 请求。

## 生产热更新与回退

本次最终实时页热更新前创建了备份：

```text
/srv/business-cn/state/hotfix-realtime-original-window-20260928T041250Z
```

生产最终回读的三个文件 SHA-256：

```text
market.css       55fbf6212e3b304932cedc8c66c4c00820f69a7b3cd290ca6b0249be54e26865
realtime.js      9fc9cfe51357ab0ffbbb67fbe01707a78ec61ee5965dc89581a43b66eb2e56d2
realtime/index.html caaf7eb5707fd95582a9ff7344ef9f724653b20f3a8499f205187044c8e19767
```

此前本 session 还创建过以下市场登录和布局备份，仍保留作回退证据：

```text
/srv/business-cn/state/hotfix-market-local-login-20260928T011444Z
/srv/business-cn/state/hotfix-market-local-login-20260928T011806Z
/srv/business-cn/state/hotfix-realtime-grid-20260928T013553Z
```

回退或再次热更新前，必须先读取当前容器文件并重新建立带时间戳的备份。不要用 `git reset`、`clean`、`stash` 覆盖其他会话改动。

## 已完成验证

本地：

```bash
cd /home/ishelwsl/src/AliECS-WebDock/AliECS
node --check services/public-web/market/realtime.js
.venv/bin/python -m pytest -q tests/test_market_frontend.py tests/test_market_ingest_load.py
```

结果：`7 passed, 1 skipped`；JavaScript 语法检查通过。

生产：

- 三个市场入口静态页面返回 HTTP 200；
- 未认证访问实时 API 返回 401；
- 临时市场账号登录后实时 API 返回 200；
- `window_minutes=5/10/15` 分别返回对应时间窗口；
- Chrome 实测实时页默认值为 5 分钟；
- Chrome 实测 `#contracts` 为 `realtime-grid`，桌面宽度下形成三列；
- 实测 8 个合约窗口，每个窗口含 6 个右侧价格标签和中心价格标签；
- 实测对冲排序节点位于合约区域之后；
- 生产 JS 包含 `attributionLogo:false`。

测试时 API 返回 8 个最新报价、8 个价格带、8 个挂单和 16 条候选排序。某次验证的 `series` 为空，因此如果当时行情源没有窗口内历史曲线点，图中线条可能没有点；这不能解释为前端删除了线条。需要继续观察有实际历史点的交易时段。

## 临时账号与安全边界

本 session 创建过临时账号 `market-debug-1bd01a`，只有 `market.read` 权限。密码没有写入本文和 Git。调试完成后应删除该账号；不要把密码写入仓库、日志、截图或新提交。

## 当前交接状态

本 session 已完成上述代码提交、生产热更新和验证。实时页最终以 AliECS 提交 `148087a` 及 txecs 容器当前文件为准；交接文档本身由提交 `d72f045` 新增。临时账号 `market-debug-1bd01a` 仍是调试账号，密码未写入仓库。
