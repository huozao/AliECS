# AI Handoff Rules
> ⚠️ 本文档部分内容已过时（三主机→四设备）。设备事实以 `docs/fleet.md` 为准，提交/部署授权以工作区顶层 AGENTS.md 为准；本文仅作历史参考。

Read this before changing deployment, OpenClaw, WebDock, Weixin or Feishu account behavior, ECS operations, or cross-host debugging.

## First Decision

Classify the task first:

```text
development
deployment
runtime operations
incident debugging
OpenClaw/Weixin/Feishu account management
WebDock/browser management
data/worker/background processing
```

Then choose the host:

```text
code and local validation -> 开发机
public app and OpenClaw control plane -> 服务器
browser, ChatGPT session, heavy workers -> 旧电脑
```

## Naming Rules

- Use `开发机`, `服务器`, and `旧电脑` exactly.
- If a task mentions "server", clarify whether it means `服务器` before changing anything that could affect production.
- If a task mentions "old computer", treat it as `旧电脑`.
- When summarizing a fix, include the affected host and repository.

## Repository Rules

- AliECS code source: `git@github.com:huozao/AliECS.git`
- AliECS 开发机 checkout: `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS`
- AliECS 服务器 checkout: `/root/AliECS`
- WebDock code source: `https://github.com/huozao/webdock.git`
- WebDock 开发机 checkout: `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\webdock`
- WebDock 旧电脑 checkout: `/opt/webdock`
- OpenClaw runtime source: `https://github.com/openclaw/openclaw.git`
- OpenClaw 服务器 runtime path: `/root/openclaw`

Do not edit the wrong repository:

- Changes to `openclaw-bridge` belong in AliECS.
- Changes to WebDock browser/API behavior belong in the WebDock repository.
- Changes to OpenClaw itself are upstream/runtime maintenance, not AliECS app work.

## AI Client Execution Policy

- Codex desktop, Claude desktop, and Claude terminal follow the same `开发机` workflow in this project.
- When the user explicitly asks for or authorizes commit, push, deploy, or verification, these clients may complete the corresponding GitHub and runtime steps themselves.
- Do not stop at "ask the user to submit/deploy manually" after such authorization.
- Before pushing, confirm git status, branch, remote URL, and that `.env`, `logs`, `browser_data`, `_references`, real secrets, browser state, and production runtime files are not being committed.
- Run all `.git`-writing commands serially.
- Documentation-only changes usually do not require a production deploy. If a push triggers GitHub Actions, or the user explicitly asks to deploy, continue through Actions and runtime verification.

## Non-Negotiable Boundaries

- Do not move Chrome or ChatGPT browser state back to `服务器`.
- Do not put OpenClaw official Weixin plugin account state on `旧电脑`.
- Do not make AliECS deployment scripts manage OpenClaw, WebDock, VPN, or global Docker cleanup.
- Do not run global `docker system prune` on `服务器`.
- Do not commit production secrets, tokens, private keys, browser profiles, or runtime account state.
- Do not treat `开发机` as a production host.

## Standard Development Loop

```text
1. Edit code on 开发机 with Codex desktop, Claude desktop, Claude terminal, or a human operator.
2. Validate locally with the smallest relevant command.
3. Use local Docker validation for app, Docker, or deployment-impacting changes.
4. Commit directly to main when the user asks or authorizes an AI client to submit/push in this single-maintainer repo.
5. Push main to GitHub.
6. GitHub Actions builds and deploys AliECS to 服务器.
7. Verify AliECS health on 服务器.
8. Verify WebDock/OpenClaw path if the change affects Weixin or Feishu replies.
9. Confirm behavior from Weixin A/B/C or Feishu when needed.
```

## Cross-Host Debug Order

For Weixin or Feishu reply issues, check in this order:

```text
1. 服务器 OpenClaw channel status
2. 服务器 openclaw-bridge: curl -fsS http://127.0.0.1:18080/v1/models
3. 服务器 reverse tunnel: curl -fsS http://127.0.0.1:11800/healthz
4. 旧电脑 WebDock API: curl -fsS http://100.97.176.57:18000/healthz
5. 旧电脑 WebDock container: docker ps; docker logs webdock
6. 旧电脑 noVNC: http://100.97.176.57:6080/vnc.html
```

Do not start by redeploying AliECS unless evidence points to AliECS code or deployment.

For Feishu specifically, also confirm:

```text
1. channels.feishu is enabled/configured/running
2. channels.feishu.connectionMode is websocket
3. channels.feishu.dmPolicy is open and allowFrom contains "*"
4. channels.feishu.groupPolicy is open
5. channels.feishu.requireMention is false if all group messages should auto-reply
6. channels.feishu.appSecret is an env SecretRef, not a pasted repository secret
```

## Evidence Required Before Fixing

Collect evidence before changing anything:

- exact user-visible error
- affected host: `开发机`, `服务器`, or `旧电脑`
- affected repository: AliECS, WebDock, or OpenClaw
- service status and port health checks
- relevant recent logs
- whether the issue started after deploy, reboot, network change, or account login
