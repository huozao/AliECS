# Couple Memory 重建上线记录（2026-06-15）

## 目标

- `/couple/` 保持单一 App 体验：Dashboard、回忆列表/详情、地图、相册、纪念日、愿望清单、分享页。
- `/map/` 不再跳 AdventureLog，改用 App 内 Leaflet 与 `/v1/map/memories`。
- 直传照片继续走 `STORAGE_DRIVER=webdock`，`local` 仅作为开发/兜底；Immich 选片路径由 `IMMICH_ENABLED` 控制，默认关闭。

## 本次代码变更

- `db/migrations/0016_couple_memory_rebuild.sql`：幂等补列、补索引、登记版本、为 Immich asset 绑定增加唯一索引。
- `services/backend-api/app/immich_client.py`、`services/backend-api/app/main.py`：新增 Immich 资产搜索/缩略图代理，批量幂等绑定 `asset_ids`。
- `services/public-web/couple/index.html`、`services/public-web/map/index.html`、`services/public-web/memories/detail.html`：收回地图/相册入口，恢复地图页，详情页按 flag 展示 Immich 选片。
- `deploy/ecs/runtime.env.example`、`deploy/ecs/release-meta.env.example`、`local/docker-compose.local.yml`、`docs/env-matrix.md`：补齐 Couple 存储与 Immich 示例配置。

## 部署前检查

```bash
python scripts/validate_version.py
PYTHONPATH=. pytest tests/test_adventurelog_frontend.py tests/test_couple_rebuild_static_contract.py tests/test_couple_immich_client.py tests/test_couple_immich_assets.py tests/test_couple_memory_helpers.py tests/test_couple_local_photo_storage.py tests/test_couple_webdock_photo_storage.py tests/test_couple_oss_photo_storage.py -q
docker compose -f local/docker-compose.local.yml config > /dev/null
docker compose --env-file deploy/ecs/runtime.env.example -f deploy/ecs/compose.prod.yml config > /dev/null
bash -n deploy/ecs/deploy.sh && bash -n deploy/ecs/migrate.sh && bash -n deploy/ecs/healthcheck.sh && bash -n deploy/ecs/rollback.sh
```

## 线上验证

```bash
ssh aliecs 'cd /root/AliECS && docker compose -f deploy/ecs/compose.prod.yml ps'
ssh aliecs 'cd /root/AliECS && bash deploy/ecs/migrate.sh'
ssh aliecs 'cd /root/AliECS && bash deploy/ecs/post-deploy-smoke.sh'
ssh aliecs 'curl -fsS https://www.hydwang.xyz/api/healthz'
ssh aliecs 'curl -fsS https://www.hydwang.xyz/couple/ | head'
ssh webdock 'curl -fsS http://100.97.176.57:18000/healthz || curl -fsS http://100.97.176.57:18000/'
```

## 人工/ops 步骤

- `IMMICH_ENABLED=false` 是默认上线状态；开启前需要人工完成 Immich 首管账号、API key、DNS/TLS，并把真实 `IMMICH_API_KEY` 写入 ECS runtime env，不能提交到 git。
- AdventureLog 仅保留为历史迁移源；如需实跑迁移，先 dry-run `python -m scripts.adventurelog.migrate_memories` 生成对账，再确认是否 `--apply`。
- 若部署后 `/couple/` 异常，先将 `COUPLE_FEATURE_ENABLED=false` 回退入口，再按 `deploy/ecs/rollback.sh` 回滚镜像。
