# Couple Memory 当前实现与收尾记录（2026-09-06）

## 当前产品边界

- Couple 只承载情侣空间；AdventureLog 保持独立入口。
- Immich 是照片/视频资产底座。Couple 保存回忆文字、关系业务数据和 `asset_id` 引用，不重复保存 Immich 原图。
- 每个 Couple 用户绑定自己的 Immich API key，由 backend 加密保存并代为调用。
- Couple 选片默认浏览当前用户全部照片，并支持个人库、家庭相册和任意 Immich 相册；选择个人库照片后自动加入配置的家庭相册。

## 交互约定

- 首页以地图和关系概览为主；发布回忆、纪念日、愿望清单、空间管理均默认折叠。
- 点击卡片先看已有明细，再用“新增”展开统一编辑器；新增、编辑、取消和错误提示保持一致。
- 回忆详情将文字、地点、封面、本地媒体和 Immich 选片放在同一编辑上下文中。
- 纪念日支持纪念日/生日、公历/农历和闰月标记；保存农历时校验公历对应日期。
- 回忆未填写日期时，有效日期按“回忆日期 → Immich 拍摄日期 → 本地照片拍摄日期”计算，并用于列表、筛选、地图排序和时间线。

## 地图与媒体经验

- Couple 地图只显示精选回忆点，不扫描 Immich 全量 GPS。
- Leaflet 外部瓦片必须逐瓦片重试，并准备无 key 的备用源；CARTO `API KEY REQUIRED` 水印不可作为正式兜底。
- Immich 缩略图通过 backend 认证代理加载；点击后在原位置替换为原图，不弹窗、不触发下载。

## 部署经验

- 线上确认优先使用静态文件/单容器热补丁；确认后再走正式镜像部署。
- 热补丁成功后必须把改动回灌 Git，否则 compose 重建会丢失。
- 数据库字段变更必须使用幂等迁移；本次农历字段迁移为 `0054_couple_lunar_anniversaries.sql`。

## 本次正式发布证据

- Couple 实现提交：`6686dec feat(couple): complete memory workspace interactions`。
- GitHub Actions 发布运行：`34023327057`，目标为 `business-cn`，构建、TCR 镜像同步和部署作业均成功。
- 发布后 `business-cn-backend-api-1` 健康检查通过；`public-web` 与 backend 均完成滚动重启。
- 发布后公网探针：`/couple/`、`/memories/`、`/map/` 均返回 HTTP 200。
- 后续若继续改动 Couple，仍按“热更新确认 → 记录结果 → 正式镜像部署”的顺序；只改文档时无需重新发布业务容器。
