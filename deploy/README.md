# 部署目录导航

- `ecs/`：角色 Compose、部署、迁移、健康检查、回滚和镜像镜像同步脚本。
- `openclaw-bridge/`：bridge 镜像构建上下文；测试位于仓库 `tests/`。
- workflow、目标设备、自动/手工触发边界统一查 `docs/runbooks/deploy.md` 和 `.github/workflows/release-deploy.yml`。
- 当前部署路径、运行角色和验证命令统一查 `docs/fleet.md`；不得从历史目录名推断。
- `ecs/install-host-gateway-proxies.sh` 为 backend 容器安装到宿主机回环服务的命名代理；
  WebDock 照片 11800 仍由 `ecs/install-webdock-tunnel-proxy.sh` 管理，以保持旧版本回滚兼容。
<!-- nav-check: deploy/ecs/install-host-gateway-proxies.sh -->
<!-- nav-check: deploy/ecs/install-webdock-tunnel-proxy.sh -->

部署产物以 GitHub Actions run/attempt 标识，源码以 commit SHA 标识，运行镜像以 OCI digest 标识。回滚前先记录当前 deployment manifest 和 digest。
