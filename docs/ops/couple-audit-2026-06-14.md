# Couple Memory 图片与端点巡检

## 本轮修复

- `LocalPhotoStorage` 默认目录从易失 `/tmp/aliecs-uploads` 改为容器持久挂载点 `/app/uploads`。
- 新增 `GET /uploads/{name}`，用于读取本地存储图片，带目录穿越防护。
- `STORAGE_DRIVER=webdock` 时，webdock 存储返回 502（不可达/上游错误）会回退到本地持久目录；webdock 正常时仍作为主存。
- `deploy/ecs/compose.prod.yml` 已给 `backend-api` 增加 `LOCAL_UPLOAD_DIR=/app/uploads` 和 `uploads:/app/uploads` 命名卷。

## 巡检范围

- `/v1/memories*`：记忆 CRUD、归档、分享，不直接写易失文件。
- `/v1/photos/upload`：上传后写 `photos.original_storage_url/display_url/thumbnail_url/storage_driver`；本轮已补齐本地取回与持久化。
- `/v1/photos/content/{key}`：webdock 代理取回，不依赖本地 `/tmp`。
- `/v1/photos/{photo_id}` 与删除：按 `storage_driver` 分支清理 local/webdock；OSS 删除已有分支保护。
- `/v1/memories/{memory_id}/immich-assets`：Immich 资产绑定走 `couple_memory_assets`，不存本地原图。

## 待人工/ops

- 部署 compose 后 `uploads` 命名卷才生效。
- 旧 `/tmp/aliecs-uploads` 内若已有历史图片，容器重启后可能已丢失；如仍在旧容器文件系统，需要人工一次性拷入新卷。
