## 变更摘要

- 本次改动解决的问题：
- 主要修改文件：

## 影响评估（必填）

- 是否影响本地运行链路（public-web/admin-ui/backend-api/postgres）：是 / 否
- 是否影响 ECS 部署链路：是 / 否
- 是否影响数据库结构或迁移：是 / 否
- 是否影响 GitHub Actions：是 / 否

## AI 导航影响（必填，保留机器可读字段）

- 路径/模块/函数、配置键/环境变量/API/端口/服务名是否变化：
- 构建/部署/回滚/备份/恢复命令是否变化：
- 功能地图、fleet、project navigation、runbook、README 是否受影响：

Nav-Impact: updated

如无需更新，把上一行改为 `Nav-Impact: none`，并填写：

Nav-Impact-Reason: <无需更新时填写依据>

## 验证记录（必填）

请粘贴你实际执行过的命令与结果：

- [ ] `bash -n deploy/ecs/deploy.sh`
- [ ] `bash -n deploy/ecs/migrate.sh`
- [ ] `bash -n deploy/ecs/healthcheck.sh`
- [ ] `bash -n deploy/ecs/rollback.sh`
- [ ] `docker compose -f local/docker-compose.local.yml config`

如未执行，请说明原因与替代验证方式。

## 前端可执行性验证（如改动前端必填）

- [ ] 已检查明显 JS 语法/作用域错误
- [ ] 已完成至少一次登录或核心入口 smoke（含请求触发）
- [ ] 如无法浏览器自动化，已说明影响与补救动作

## 配置与安全检查（必填）

- [ ] 新增/修改环境变量已同步文档与示例配置
- [ ] 未提交真实密钥、密码、token、私钥
- [ ] 线上验证仅使用测试账号

## 回退方案（必填）

- 回退方式（tag / commit / 脚本）：
- 回退后需要额外检查项：

