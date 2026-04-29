# AliECS 设计系统（Claude 风格参考）

> 目标：为 `services/public-web/index.html` 提供可持续维护的视觉规范。  
> 参考来源：VoltAgent/awesome-design-md 中 Claude 的 `DESIGN.md`（设计语言参考，不做像素级复刻）。

## 1) 视觉气质
- 克制、温暖、清爽、工具型。
- 不做重动画、不做大面积炫技渐变。
- 让“部署流程与文档入口”成为信息中心。

## 2) 色彩与层次
- 背景主色：暖纸色 `#f5f4ed`
- 卡片背景：`#faf9f5`
- 主文本：`#141413`
- 次文本：`#5e5d59`
- 品牌强调（CTA）：`#c96442`
- 边框：`#e8e6dc`

## 3) 字体与排版
- 标题：优先 serif（`Georgia` 兜底）
- 正文：`Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial`
- 代码/命令：`ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`
- 标题行高偏紧，正文行高偏舒适（1.55~1.65）

## 4) 组件风格
- 卡片：8~16px 圆角，轻边框，极浅阴影。
- 按钮：
  - 主按钮：terracotta 实色
  - 次按钮：暖灰底 + 深色文本
- 导航：极简，固定顶部，强调“文档入口”。

## 5) 页面信息结构（首页）
1. 顶部导航 + Hero（业务入口型，不是宣传型）
2. 项目简介（定位、当前边界、后续承载）
3. 业务功能入口区（配方查询/美的需求/库存/Admin UI）
4. 技术与系统区（Public Web、Admin UI、Backend API、Data Layer）
5. 自动部署流程简述（次级信息，不喧宾夺主）
6. 文档与 GitHub 入口


## 6) 禁止事项
- 不做过长营销页。
- 不堆叠复杂视觉特效。
- 不用高饱和霓虹色。
- 不把技术信息藏在二级页面。

## 7) 维护约定
- 新增页面或改版时先对照本文件。
- 若需要改风格，先更新本文件再改代码。


## 8) 首页功能预留原则
- 首页必须覆盖并可跳转到：Public Web、Admin UI、Backend API、Deploy Pipeline、Data Layer、Future Sync Pipeline。
- 所有“未实现功能”使用 `预留扩展` 标签，不伪装成已上线。
- 结构上优先“工具信息架构”：Hero -> 能力概览 -> 功能模块 -> 自动部署流程 -> 文档入口。
