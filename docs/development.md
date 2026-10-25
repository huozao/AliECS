# 开发与验证约束

本文件是 AliECS 各 AI 客户端共同使用的开发约束正本。由 [AGENTS.md](../AGENTS.md) 按任务进入，修改对应模块前读对应节；无需父工作区文件或自动加载插件。历史原文见 [治理归档](migrations/2026-09-13-guidance-history.md)。

## 本地开发

先沿 [项目地图](project-ai-map.md) 找业务入口、数据来源、判断与输出；保持现有架构、命名和工具链，不引入无必要抽象或依赖。更改环境变量时说明用途、缺失行为并同步示例，真实值不进仓库。迁移使用新文件，保持可重复执行、失败可定位；删除数据、重命名字段、权限调整要说明风险。

本地验证只使用 `local/docker-compose.local.yml` 和含测试值的 `local/.env.local`，不读取生产 env、不连生产库。优先现有虚拟环境和项目脚本；缺依赖或无法执行时说明原因，不把未运行当通过。

| 影响面 | 最小验证入口 |
|---|---|
| Python 业务 | 相关 pytest 文件；全量为 `python -m pytest tests` |
| 本地链路 | `scripts/local-smoke-test.sh` 或 `scripts/local-smoke-test.ps1` |
| shell | `bash -n deploy/ecs/{deploy,migrate,healthcheck,rollback}.sh`，按改动补相关脚本 |
| Compose | `docker compose -f local/docker-compose.local.yml config > /dev/null` |
| 前端 | JS 语法/作用域检查，加核心入口点击触发请求的 smoke |
| 导航治理 | 本文「文档闭环」命令 |

本地或合成验证不等于生产与真实账号业务证明。生产验证入口、授权范围和回退在 [deploy](runbooks/deploy.md)，职责与路径在 [fleet](fleet.md)。

## 测试与隔离

- 全量使用 pytest。`unittest discover` 只收 TestCase，不能代表裸函数式测试；单独的纯 unittest 文件可用其已有命令。
- 比较测试收集数前确认两个检出使用相同 base commit；有差异时比较测试 ID，不能只比较总数。
- 多个服务都有顶层 `app` 包。测试切换服务前清理 `sys.modules` 中 `app` 与 `app.*`，并加入目标服务根目录；结束后清理并恢复 `sys.path`。不能让某个服务先占住包名后，其他测试延迟导入错误服务。
- TestCase 使用成对的 setup/teardown 隔离；裸函数式测试在模块导入 helper 或 fixture 中处理。参考 `tests/test_backend_wecom_kf.py` 的 `_ensure_backend_app`：只有已占用的包不是目标服务时才清理，避免无条件重新导入导致模块身份改变、mock patch 打空。
- 测试改变 `os.environ` 必须恢复；使用 monkeypatch、patch.dict 或等价 fixture。不要在模块级或 helper 中永久 setdefault。涉及此类隔离时同时跑相关文件组合，单文件成功不能排除顺序污染。

```python
# TestCase 协议示意；BACKEND_ROOT 应由本测试文件定位。
@classmethod
def setUpClass(cls):
    cls._previous_path = list(sys.path)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    sys.path.insert(0, str(BACKEND_ROOT))

@classmethod
def tearDownClass(cls):
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    sys.path[:] = cls._previous_path
```

## 前端

适用于 `services/public-web/` 和 `services/admin-ui/`。

- 区分服务端返回、浏览器实际加载和用户可见行为。排除旧缓存可用无痕窗口或资源版本 URL；已缓存副本不会因服务器后来加 no-store 立即失效。查看 access log 是否出现该次请求以及是否为 304，不能只用服务端 curl 宣称用户验证完成。
- 初始化默认值只在首次进入模式、切换查询等明确状态转换执行，不能每次 render 都把用户主动清空当成尚未初始化。需要时增加显式初始化状态，并显示选择计数。
- JS 语法检查后，用页面实际依赖执行 smoke；无账号的本地探针可放页面同目录副本，加载后伪造状态并将结果写入 DOM，再通过 headless Chrome dump-dom 读取。按对齐方式验证 bottom 或中心线等布局关系；不要用不相干的 top 值判断换行。生成探针时注意字符串转义，用完移除临时文件。
- `Failed to fetch` 且确认目标服务 access log 无对应请求时，先取浏览器 Network（Preserve log）、Console 与客户端网络证据。服务端无报错只能缩小范围，不能据此猜改后端。
- 静态热更新涉及共享 `common/` 时必须同步依赖；只拷 HTML 可能使第一行脚本因缺少共享对象而停止。热更新本身仍需要部署授权，操作与回灌要求见 [deploy](runbooks/deploy.md)。
- 配方业务口径和共享模块契约另见 [formula](runbooks/formula.md)。

## 通知

适用于 `services/backend-api/app/notify/`、`services/doc-sync-worker/app/notify_client.py` 与通知生产者。完整投递契约及排障见 [notify](runbooks/notify.md)。

- 测试日志、卡片或序列化新增字段时使用生产 formatter/serializer；上游接受任意字段不等于下游白名单真的输出了它。必须读回实际输出，不能只检查输入记录或注册成功。
- 重试策略按业务成因分类：本通道消息校验的 400/422 可作为无效消息处理；部署接线的 401/403/404 可能随凭据、来源或路由就绪而恢复，不能笼统丢弃全部 4xx。队列应有上限并淘汰最旧记录，配置好凭据和路由后再启动首轮。
- 出站统一经过中枢；HTTP 200、outbox 存在、timer 活跃不等于送达。验收实际 `notify_deliveries` 状态及目标渠道凭证，用户实际收到是更强证据。
- 不在公开仓保存目标身份和凭据；渠道原生载荷不能携带收件人、请求 URL 或密钥。

## 忽略规则

修改任一 `.gitignore` 时，白名单必须逐级放行到文件层。父目录被忽略时，内部单独的 `!` 无法恢复文件。用 `git check-ignore -v <具体文件>` 定位生效规则，同时验证应进入的源码可跟踪，以及 secrets、本地配置、缓存、运行数据仍被忽略；不要只验证一个方向。

## 文档闭环

按实际行为变化回查路径、符号、配置键、API、部署/回滚、备份与专项 runbook；有影响直接更新原文，无影响记录依据，不另造一份重复说明。地图只维护功能到代码/验证入口，设备职责和运行位置留给 [fleet](fleet.md)，时间点材料留在 plans/migrations 并注明历史身份。

CI 的 Python 版本与生产镜像一致：`ci.yml` 两个 job 都用 `actions/setup-python` 固定 3.12，validate 另有一步核对 `services/backend-api/Dockerfile` 的 `python:<版本>`；升级生产 Python 时同批修改两处。不要依赖 `ubuntu-latest` 自带的系统 Python（2026-10-25 runner 换成 26.04 后变为 3.14，所有 PR 在安装依赖时失败）。

PR 正文写 `Nav-Impact: updated`，或同时写 `Nav-Impact: none` 和 `Nav-Impact-Reason: <依据>`；commit message 不能替代 PR body。`.github/workflows/ci.yml` 在 `opened / synchronize / reopened / ready_for_review / edited` 触发，修改正文会建立使用新正文的检查；旧 run rerun 仍保留原事件载荷，不需要为了更新正文推空提交或关闭重开 PR。

治理文档在 `.navigation-check.json` 的 documents 注册。以反引号点名本仓 Python 的非下划线模块/类级定义时，在点名的文档旁写 inline 断言；不把符号清单与说明分开存。示意必须在围栏内：

```text
<!-- nav-check-python: 相对路径.py:符号名 -->
<!-- nav-check: 仓库相对路径 -->
```

不为函数内局部变量、同名的数据库表/JSON 字段/API 参数/环境变量或历史归档材料添加 Python 符号断言；断言保护的是文档明确点名的代码。独立克隆从本仓根目录执行：

```bash
python3 scripts/check_navigation.py
python3 -m unittest discover -s tests -p "test_*navigation*.py"
python3 -m unittest discover -s tests -p test_nav_impact.py
python3 -m unittest discover -s tests -p test_doc_freshness.py
```

代码结构改变时按 PR base/head 执行 `scripts/check_doc_freshness.py --range <base> <head> --staleness`。收尾复验一条「功能 → 代码 → 验证命令」链路；涉及运行时再经 fleet 到目标设备验证。独立克隆不要求父工作区文件。完成后报告修改、运行/部署/数据库影响、已执行与未执行验证及回退方法。
