# Three-Host Architecture
> ⚠️ 本文档已被 `docs/fleet.md`（四设备单一事实源）取代，仅作历史参考。设备命名、主备、端口以 fleet.md 为准。

This project uses three fixed host names in all documentation, issues, commits, and AI handoff notes. Do not replace them with vague words such as "machine", "remote", "node", or "box" unless the context is already explicit.

## Standard Terms

| Standard term | Meaning | SSH alias | Main responsibility |
|---|---|---|---|
| 开发机 | Current Windows development computer | local PowerShell | Code editing, local validation, GitHub, remote coordination |
| 服务器 | Alibaba Cloud ECS instance | `aliecs` | Public ingress, AliECS, Postgres, OpenClaw, `openclaw-bridge`, `sing-box` |
| 旧电脑 | Old Ubuntu laptop | `webdock` | WebDock, Chrome/ChatGPT session, noVNC, Playwright, heavy/background work |

When writing instructions for AI:

- Use `开发机` for `C:\Users\ishel\Desktop\编程总库\...` paths.
- Use `服务器` for ECS paths such as `/root/AliECS`, `/root/openclaw`, and `/opt/openclaw-bridge`.
- Use `旧电脑` for Ubuntu laptop paths such as `/opt/webdock`.

## Repository and Path Map

| Runtime area | GitHub source | 开发机 checkout | Runtime checkout/path | Notes |
|---|---|---|---|---|
| AliECS app and deployment | `git@github.com:huozao/AliECS.git` | `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS` | 服务器: `/root/AliECS` | Source of `public-web`, `admin-ui`, `backend-api`, `doc-sync-worker`, ECS deploy scripts, and `openclaw-bridge` source |
| WebDock browser relay | `https://github.com/huozao/webdock.git` | `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\webdock` | 旧电脑: `/opt/webdock` | Source of WebDock API, browser profile handling, noVNC/VNC runtime, and laptop service files |
| OpenClaw runtime | `https://github.com/openclaw/openclaw.git` | No normal development checkout in AliECS workflow | 服务器: `/root/openclaw` | Runtime dependency. Do not edit from AliECS unless explicitly doing OpenClaw maintenance |

## Host Responsibilities

### 开发机

Owns:

- Codex development work
- Local Docker validation
- Git commits and pushes to GitHub
- SSH/Tailscale coordination for `服务器` and `旧电脑`

Must not own:

- Long-running production services
- The only copy of runtime state
- Browser sessions required by production traffic

### 服务器

Owns:

- Public Nginx and TLS routing
- AliECS website and API stack
- Postgres primary database
- OpenClaw gateway and official Weixin plugin accounts A/B/C
- OpenClaw Feishu channel
- `openclaw-bridge`
- `sing-box` proxy/VPN control plane
- Lightweight forwarding to `旧电脑`

Must not own:

- Chrome
- ChatGPT browser login state
- Playwright workloads
- Large file processing
- Heavy background workers

Default paths and services:

- AliECS app root: `/root/AliECS`
- OpenClaw stack: `/root/openclaw`
- Bridge install path: `/opt/openclaw-bridge`
- Bridge service: `openclaw-bridge.service`
- Bridge API: `127.0.0.1:18080`
- WebDock reverse tunnel endpoint: `127.0.0.1:11800`

### 旧电脑

Owns:

- WebDock
- Chrome / ChatGPT browser profile and login state
- VNC / noVNC
- Playwright and browser automation
- Large downloads, cache, temporary files, backups, and exports

Must not own:

- Public Nginx/TLS ingress
- AliECS primary API
- Postgres primary database
- OpenClaw official Weixin plugin gateway

Known endpoints:

- Tailscale address: `100.97.176.57`
- WebDock API: `100.97.176.57:18000`
- WebDock noVNC: `100.97.176.57:6080`
- Runtime checkout: `/opt/webdock`

## Runtime Chain

Weixin and Feishu traffic should flow through this path:

```text
Weixin A/B/C or Feishu
-> 服务器 OpenClaw channel plugin
-> 服务器 OpenClaw gateway
-> 服务器 wechat-bridge provider
-> 服务器 openclaw-bridge at 127.0.0.1:18080 / Docker gateway
-> 服务器 reverse tunnel endpoint at 127.0.0.1:11800
-> 旧电脑 WebDock at 100.97.176.57:18000
-> 旧电脑 Chrome / ChatGPT web session
-> reply back through the same path
```

The boundary is strict: OpenClaw, Weixin account state, and Feishu channel state stay on `服务器`; ChatGPT browser state stays on `旧电脑`.
