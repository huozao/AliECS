# Codex AdventureLog 交付总结

## Phase 完成度

- Phase A 部署物料：完成。infra 仓可写，compose/env/tunnel/nginx/runbook 已直接提交到 infra 仓。
- Phase B couple 前端：完成。`/couple/` 地图足迹与相册模块改为 AdventureLog 外链；旧 `/map/` 保留路由并提示后跳转。
- Phase C 迁移 ETL：完成离线 CODE。已实现 transform、REST 薄客户端、迁移驱动、dry-run 默认、幂等跳过、Immich asset 关联占位与对账报告输出。
- Phase D OPS：未执行。清单已原样写入 `docs/ops/adventurelog-deploy-2026-06-14.md`，等待人工部署。

## 交付物清单

AliECS：

- `scripts/adventurelog/transform.py`
- `scripts/adventurelog/al_client.py`
- `scripts/adventurelog/migrate_memories.py`
- `tests/test_adventurelog_migration.py`
- `tests/test_adventurelog_frontend.py`
- `services/public-web/couple/index.html`
- `services/public-web/map/index.html`
- `docs/ops/adventurelog-deploy-2026-06-14.md`
- `docs/ops/codex-adventurelog-2026-06-14-summary.md`

infra：

- `laptop/adventurelog/docker-compose.yml`
- `laptop/adventurelog/.env.example`
- `laptop/adventurelog-tunnel.service`
- `server/nginx/adventure.hydwang.xyz.conf`
- `server/nginx/adventure-media.hydwang.xyz.conf`
- `docs/runbook-adventurelog.md`

## 待人工 OPS 指引

- 先按 `docs/ops/adventurelog-deploy-2026-06-14.md` 完成旧电脑内存核对、`.env` 密钥填充、DNS、compose、隧道、nginx/TLS、关注册、建用户和 Immich 集成。
- 迁移实跑前先执行 `python -m scripts.adventurelog.migrate_memories` 做 dry-run，对账报告确认无误后才加 `--apply`。
- 迁移脚本真实运行前，需按 AdventureLog `v0.12.1` 文档或浏览器请求核对 adventure 字段名、创建端点、Immich asset 关联端点和 `DISABLE_REGISTRATION` 行为。

## 本轮验证

- `PYTHONPATH=. pytest tests/test_adventurelog_migration.py -v`：5 passed。
- `PYTHONPATH=. pytest tests/test_adventurelog_frontend.py -v`：2 passed。
- `PYTHONPATH=. pytest tests -q`：220 passed, 2 skipped, 5 warnings。
- `docker compose config`：使用临时目录把 `.env.example` 复制为占位 `.env` 后验证 compose 解析通过；仓库未留下 `.env`。
- `PYTHONPATH=. pytest -q`：未作为通过项；收集 `services/tplus-sync-worker/tests` 时缺少该子项目路径，报 `ModuleNotFoundError: config` / `ModuleNotFoundError: tplus_datahub`。

## 剩余风险

- AdventureLog REST API 字段和端点仍是实施时核对项，代码中已集中标注 TODO。
- 旧电脑内存余量、隧道端口占用、nginx/TLS 和 Immich API key 权限必须在 Phase D 人工确认。
