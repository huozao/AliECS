# AliECS 下一步改进方向（2026-05）

> 目标：在不破坏现有边界（public-web / admin-ui / backend-api / postgres / compose / ECS / migration / healthcheck / Actions）的前提下，优先做可上线、可回滚、可观测的增强。

## 1. P0（1~2 周）先补齐“可发布安全线”

### 1.1 鉴权与会话最小增强
- 把当前自签 token 机制升级为标准 JWT（HS256 起步），并保留短期兼容读取旧 token 的过渡逻辑。
- 登录 token 增加 `jti`，并落库实现基础“主动失效”能力（例如管理员重置密码后踢下线）。
- 对 `AUTH_TOKEN_SECRET` 增加长度检查（例如 >= 32）与启动期告警，避免弱密钥上线。

### 1.2 上传与静态资源安全
- 限制上传 MIME + 后缀双校验；拒绝伪造扩展名文件。
- `LOCAL_UPLOAD_DIR` 增加磁盘占用阈值告警，防止 ECS 磁盘被照片写满。
- 为 `/uploads/*` 增加只读映射策略，防止被误写。

### 1.3 ECS 发布流程防误操作
- `deploy.sh` 增加镜像 tag 格式校验（仅允许 `vX.Y.Z` 或 `vX.Y.Z-rc.N`）。
- 迁移执行前打印目标数据库主机/库名（脱敏）与镜像 tag，便于审计。
- 把 `release-meta.env` 的关键项补充到 checklist，形成“发布前必勾选”清单。

## 2. P1（2~4 周）补齐 Couple Memory 可用闭环

### 2.1 API 业务闭环
- 新增 Couple Space 管理接口（创建空间、成员管理、成员退出）。
- 增加 Memory 归档/取消归档，避免首页列表无限增长。
- 增加 Share Link 的创建/失效接口（已建表，尚未形成完整产品能力）。

### 2.2 数据模型与迁移治理
- 新增迁移版本号表（如 `schema_migrations`），避免每次部署重复全量扫 SQL。
- 为高频查询补索引：`photos(memory_id)`、`couple_members(user_id, couple_space_id)`。
- 对 `memory_date`、`visibility`、`status` 等字段补枚举约束一致性检查。

### 2.3 前端信息架构
- `public-web` 先落地 Dashboard 静态块（时间轴、地图、纪念日、愿望清单），对应已存在表结构。
- `admin-ui` 至少补齐“用户-角色-权限”可视化与审计日志查看页，降低纯 API 运维成本。

## 3. P2（4~8 周）为生产长期运行做准备

### 3.1 对象存储替代本地原图
- 按项目约束推进：ECS 不长期存原图，切换到 OSS / S3 / R2 保存原图。
- DB 仅保存元数据与链接；缩略图可在异步任务中生成。
- 保留本地 driver 作为 dev fallback，prod 强制对象存储 driver。

### 3.2 可观测性与审计
- 增加结构化日志（JSON），包含 request_id / user / route / latency。
- 增加部署事件日志与迁移事件日志统一字段，便于追溯“谁在何时发了什么版本”。
- 健康检查拆分：`/healthz`（活性）与 `/readyz`（依赖就绪）语义继续强化。

### 3.3 CI/CD 稳定性增强
- Actions 增加 migration dry-run job（对临时 postgres 容器跑迁移）。
- 增加 backend API smoke test（登录、features、couple access、memory CRUD 最小链路）。
- 发布后自动回读 `readyz` + 关键 API，失败自动触发 rollback 并通知。

## 4. 直接发布到阿里云 ECS 的现实风险与对应改进

### 高风险项（建议本周先处理）
1. **单机单库**：postgres 与应用同机，缺少备份演练。
   - 建议：每天逻辑备份 + 每周恢复演练。
2. **本地磁盘存图**：照片增长会挤占系统盘。
   - 建议：尽快切 OSS/S3，至少先加磁盘水位告警。
3. **密钥管理靠 env 文件**：误改或泄露风险高。
   - 建议：权限最小化（`chmod 600`）+ 定期轮换策略。
4. **缺少灰度**：当前是直接切换。
   - 建议：先在 ECS 上保留 `-rc` 预发布流程，人工验收后再发正式 tag。

## 5. 推荐执行顺序（避免大改翻车）
1. 先做 P0.3（发布防误操作）+ P0.1（鉴权底线）
2. 再做 P1.2（迁移治理）
3. 然后做 P2.1（对象存储切换）
4. 最后逐步补 P1.1 / P1.3 / P2.2 / P2.3

---

如果只允许选“一个最值回票价”的动作：
**优先做“迁移版本管理 + 发布前校验 + 对象存储改造方案设计”**，这三件事能同时降低上线事故率和后续运维成本。
