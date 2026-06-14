# Codex 2026-06-14 收尾任务基线

## 分支

- AliECS：`codex/project-completion-2026-06-14`，基于 `origin/main`
- webdock：`codex/project-completion-2026-06-14`，基于 `origin/main`

## 依赖

- AliECS 执行：`pip install -q -r services/backend-api/requirements.txt openpyxl`
  - 退出码 0
  - 警告：`sse-starlette 3.4.4 requires starlette>=0.49.1, but you have starlette 0.47.3`
- webdock 执行：`pip install -q -r requirements.txt`
  - 退出码 0

## 测试基线

- AliECS：`PYTHONPATH=. pytest tests/ -q`
  - 213 passed, 2 skipped, 2 warnings
  - warnings：`bom_query.py` pandas `FutureWarning`
- webdock：`pytest -q`
  - 107 passed
