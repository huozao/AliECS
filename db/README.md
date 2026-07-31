# 数据库迁移导航

- 新结构变更只新增 `migrations/` 中的迁移，不改写已发布历史。
- 执行者是 `deploy/ecs/migrate.sh` / `deploy-role.sh`；生产部署顺序和跳过条件查 `docs/runbooks/deploy.md`。
- 文件按既有时间/序号顺序执行，迁移须可重复、失败可定位；删除、重命名和权限变化必须写回滚风险。
- 本地只用测试数据库验证；生产命令必须从 deploy runbook 进入并获授权。

```bash
bash -n deploy/ecs/migrate.sh
python -m unittest discover -s tests
```
