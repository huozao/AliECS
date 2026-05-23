# ECS single-host boundaries

This server can run multiple stacks, but AliECS deployment must only manage the AliECS stack.

## Current boundaries

AliECS owns:

- `/root/AliECS`
- Docker Compose project `ecs`
- Containers named `ecs-*`
- Local ports `127.0.0.1:8080`, `127.0.0.1:8081`, `127.0.0.1:8000`
- `/root/AliECS/deploy/ecs/release-meta.env`
- `/root/AliECS/deploy/ecs/runtime.env`
- `/root/AliECS/deploy/ecs/.release-meta`

AliECS deployment must not manage:

- `sing-box.service`
- `openclaw-bridge.service`
- Docker Compose project `openclaw`
- Docker Compose project `browser-ai-relay`
- Non-AliECS Nginx server blocks
- Global Docker pruning or system-wide cleanup

## Daily deployment flow

1. Edit and verify code locally on Windows.
2. Push reviewed changes to GitHub.
3. GitHub Actions builds versioned GHCR images.
4. GitHub Actions SSHs to ECS.
5. ECS syncs `/root/AliECS` to `origin/main`.
6. ECS optionally fetches `release-meta.env` from KMS when `ALIYUN_KMS_SECRET_NAME` is configured.
7. `deploy/ecs/deploy.sh <version>` updates only the `ecs` Compose project.
8. Post-deploy smoke checks verify the AliECS services.

## Emergency rollback

Use this when deployment succeeded but the application has a production issue:

```bash
cd /root/AliECS
bash deploy/ecs/emergency-rollback.sh
```

This switches only AliECS containers back to the previous runtime env. It does not touch OpenClaw, browser-ai-relay, sing-box, or global Nginx configuration.

After service is restored, fix or revert the GitHub commit and publish a new clean deployment.

## Nginx boundary

The host Nginx may route `hydwang.xyz` to AliECS, but AliECS application deployment should not rewrite the whole `/etc/nginx` tree.

If Nginx is versioned later, keep AliECS routing in a dedicated file, such as:

```text
deploy/nginx/aliecs.conf
```

Do not mix VPN, OpenClaw, browser relay, and AliECS routes in a single generated config.
