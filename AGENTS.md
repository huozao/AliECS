# Codex 修改原则

1. 每次修改 GitHub Actions 发布部署流程时，必须保持中文文案清晰。
2. workflow_dispatch 页面不要写死具体示例版本号，例如“例如 v2.1.4”。
3. 发布部署 workflow 必须在运行日志和 Step Summary 中显示：上一个版本、计划发布版本、当前部署版本。
4. 手动发布时，用户只需要填写计划发布版本。
5. 上一个版本由 workflow 自动从 Git tag 中识别。
6. 如果无法动态显示在 GitHub UI 输入框中，不要伪造动态 UI，应在 workflow 运行日志和 Step Summary 中显示。
7. 任何修改都不能破坏：public-web、admin-ui、backend-api、Docker Compose、ECS 部署脚本。
8. 修改发布流程后必须验证：workflow YAML 语法正确、workflow_dispatch 输入项文案正确、版本识别逻辑能运行、Step Summary 能输出版本信息。

9. GitHub Pull Request 的标题、说明、变更摘要、验证说明等在不影响程序运行的前提下必须使用中文。
