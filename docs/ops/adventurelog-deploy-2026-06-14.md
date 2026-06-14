# AdventureLog 部署 OPS 清单

Codex 本轮只完成 CODE 交付，不执行以下主机/外部人工操作。

## 部署物料落点

infra 仓可写，部署物料已放入 infra 仓：

- `infra/laptop/adventurelog/docker-compose.yml`
- `infra/laptop/adventurelog/.env.example`
- `infra/laptop/adventurelog-tunnel.service`
- `infra/server/nginx/adventure.hydwang.xyz.conf`
- `infra/server/nginx/adventure-media.hydwang.xyz.conf`
- `infra/docs/runbook-adventurelog.md`

## Phase D 上线清单（OPS，人工执行）

- [ ] 1. **旧电脑核内存余量**：`free -h` + 看 Immich/webdock 占用；不足则加 swap 或调 mem_limit。
- [ ] 2. **放部署物料**：把 `infra/laptop/adventurelog/*` 拷到旧电脑；`.env` 由 `.env.example` 复制并填真实密钥（SECRET_KEY/超管/db 口令），**不进 git**。
- [ ] 3. **DNS**：`adventure.hydwang.xyz`、`adventure-media.hydwang.xyz` 解析到 ECS 公网 IP。
- [ ] 4. **起栈**：旧电脑 `docker compose -f docker-compose.yml up -d`；`docker compose logs -f` 看三容器健康。
- [ ] 5. **隧道**：装 `adventurelog-tunnel.service` 到旧电脑、`systemctl enable --now`；ECS `ss -ltnp | grep -E '18015|18016'` 确认两端口在听。
- [ ] 6. **nginx + TLS**：ECS 放两 server 块、`certbot --nginx -d adventure.hydwang.xyz -d adventure-media.hydwang.xyz`、`nginx -t && systemctl reload nginx`；把两 server 块 + 隧道 unit 归档进 infra 仓 runbook（防重建丢失，与 OAuth 路由同类风险）。
- [ ] 7. **关注册**：核该版本注册开关；无 env 则登录 Django admin 关闭公开注册。
- [ ] 8. **建用户**：用 `DJANGO_ADMIN_*` 超管登录，建你俩 2 个账号。
- [ ] 9. **Immich 集成**：在 Immich 各生成个人 API key（asset.read/view、album.read、library.read、user.read）；在各自 AdventureLog 账号设置填 `https://immich.hydwang.xyz/api` + key；测试搜图/挂图成功。
- [ ] 10. **跑迁移**：先 `python -m scripts.adventurelog.migrate_memories`（dry-run）看对账报告；确认无误再 `--apply`；抽样 3-5 条核对地点/坐标/日期；照片"需人工"项逐个补。
- [ ] 11. **验收**（spec §9 清单全过）+ **备份**：AdventureLog PostGIS 卷纳入 restic。
- [ ] 12. **回退预案**：`docker compose down` + 摘 nginx 两块 + 停隧道 unit；自建 couple 不受影响。

## CODE 后手动验收项

- 打开 `/couple/`，确认「地图足迹」和「相册」模块卡片都新标签打开 `https://adventure.hydwang.xyz`。
- 打开 `/map/`，确认显示「地图足迹已迁移至 AdventureLog」，约 1.5 秒后跳转到 `https://adventure.hydwang.xyz`。
- 迁移脚本真实 dry-run 前，核对 AdventureLog `v0.12.1` 的 adventure 字段名、创建端点、Immich asset 关联端点和 `DISABLE_REGISTRATION` 机制。
