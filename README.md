# AliECS

业务平台源码仓：Web/API、数据同步 worker、飞书 bridge、数据库迁移和发布编排。

## AI 与人类首跳

- 工作规则：[`AGENTS.md`](AGENTS.md)
- 功能到代码、修改入口：[`docs/project-ai-map.md`](docs/project-ai-map.md)
- 设备与运行位置：[`docs/fleet.md`](docs/fleet.md)
- 部署、回滚与生产验证：[`docs/runbooks/deploy.md`](docs/runbooks/deploy.md)
- 飞书链路：[`docs/runbooks/feishu.md`](docs/runbooks/feishu.md)
- T+：[`docs/runbooks/tplus.md`](docs/runbooks/tplus.md)
- doc-sync 约束：[`docs/constraints/doc-sync.md`](docs/constraints/doc-sync.md)

文档只负责引路；源码、workflow、Compose 与只读实测负责证实。设备、IP、端口、主备和当前生产职责不得从本 README 推断。

## 本地最小验证

```bash
python -m unittest discover -s tests
docker compose -f local/docker-compose.local.yml config
```

按改动范围追加对应检查，详见 [`AGENTS.md`](AGENTS.md)。生产环境变量和数据库不得用于本地测试。

## 发布边界

- 合入 `main` 会构建镜像；仅 bridge 内容变化时会自动调用 bridge cutover。
- business-cn、candidate、edge 等业务角色由获授权的 `workflow_dispatch` 显式选择；不得写成“push 即全设备部署”。
- 源码版本以 Git commit SHA 为准，运行镜像以 OCI digest 为准。
- 变更走分支和 PR；验证通过后回查 AI 导航并在 PR 记录 Nav Impact。
