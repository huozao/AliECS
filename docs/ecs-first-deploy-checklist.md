# ECS 首次部署操作手册（可照抄执行）

> 适用：新阿里云 ECS（Ubuntu 24.04）从 0 到可自动发布。

## 0. 目标
- 目标目录：`/opt/app`
- 目标部署方式：GitHub tag -> Actions -> SSH 执行 `/opt/app/deploy/ecs/deploy.sh <tag>`
- 目标入口：宿主机 Nginx `80` 反代到 `127.0.0.1:8080/8081/8000`

---

## 1) 安装 Docker / Compose（目的：提供容器运行能力）
```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
newgrp docker

docker --version
docker compose version
```

---

## 2) 安装 Nginx（目的：统一公网入口 80/443）
```bash
sudo apt install -y nginx
sudo systemctl enable nginx
sudo systemctl start nginx
sudo systemctl status nginx --no-pager
nginx -v
```

---

## 3) 创建部署目录（目的：标准化路径，便于迁移）
```bash
sudo mkdir -p /opt/app
sudo chown -R "$USER":"$USER" /opt/app
cd /opt/app
pwd
```

---

## 4) 拉取仓库（目的：获得 deploy 脚本与配置）
```bash
cd /opt/app
git clone https://github.com/huozao/AliECS .
git rev-parse --short HEAD
```

---

## 5) 准备 release-meta.env / runtime.env（目的：定义生产运行参数）
```bash
cd /opt/app
cp deploy/ecs/release-meta.env.example deploy/ecs/release-meta.env
cp deploy/ecs/runtime.env.example deploy/ecs/runtime.env
```

编辑 `deploy/ecs/release-meta.env`（至少改这几项）：
- `POSTGRES_PASSWORD`
- `DATABASE_URL`（密码一致）
- 若 GHCR 私有：`GHCR_USERNAME` / `GHCR_TOKEN`（token 需要 `read:packages`）

验证配置文件：
```bash
grep -E '^(COMPOSE_FILE|RUNTIME_ENV_FILE|POSTGRES_USER|POSTGRES_DB|HEALTHCHECK_URL|GHCR_BASE)=' deploy/ecs/release-meta.env
```

---

## 6) 配置 SSH（目的：让 GitHub Actions 可远程执行 deploy.sh）

### 6.1 在 ECS 生成专用密钥（可选，推荐）
```bash
ssh-keygen -t ed25519 -f ~/.ssh/aliecs_actions -N ""
cat ~/.ssh/aliecs_actions.pub
```

把公钥追加到 ECS 登录用户 `~/.ssh/authorized_keys`。

### 6.2 在 GitHub 仓库设置 Secrets
- `ECS_HOST`：ECS 公网 IP
- `ECS_USER`：SSH 用户
- `ECS_SSH_KEY`：`~/.ssh/aliecs_actions` 私钥全文

---

## 7) 配置 GHCR 拉取权限（目的：ECS 可 pull 生产镜像）

如果 GHCR 镜像是私有：
1. 在 GitHub 创建 PAT（classic）并赋予 `read:packages`。
2. 在 `deploy/ecs/release-meta.env` 填：
   - `GHCR_USERNAME=<你的 GitHub 用户名>`
   - `GHCR_TOKEN=<你的 PAT>`

如果 GHCR 镜像是公开：可留空。

---

## 8) 配置 Nginx 反向代理（目的：公网 80 转发到本机服务）
```bash
sudo tee /etc/nginx/conf.d/aliecs.conf >/dev/null <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /admin/ {
        proxy_pass http://127.0.0.1:8081/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX

sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

---

## 9) 测试 deploy.sh（目的：验证完整发布链条）

> 先确保对应 tag 镜像已在 GHCR 存在。

```bash
cd /opt/app
./deploy/ecs/deploy.sh v0.1.0
```

---

## 10) 测试 healthcheck（目的：确认应用就绪）
```bash
cd /opt/app
./deploy/ecs/healthcheck.sh
curl -fsS http://127.0.0.1:8000/readyz
```

---

## 11) 测试 rollback（目的：验证故障时可恢复）
```bash
cd /opt/app
./deploy/ecs/rollback.sh
./deploy/ecs/healthcheck.sh
```

---

## 12) 首次联调 GitHub Actions 自动发布（目的：打通自动化）

在本地：
```bash
git tag v0.1.0
git push origin v0.1.0
```

在 GitHub Actions 查看工作流 `发布并部署` 是否成功。

---

## 13) 日常更新部署（以后）
1. 本地改代码 -> 合并到 `main`。
2. 打新 tag：`vX.Y.Z`。
3. `git push origin vX.Y.Z`。
4. 观察 Actions + ECS 健康检查。

---

## 14) 常用排障命令
```bash
# 部署日志
tail -n 300 /opt/app/deploy/ecs/logs/deploy-$(date +%Y%m%d).log

# 容器状态
docker compose --env-file /opt/app/deploy/ecs/runtime.env -f /opt/app/deploy/ecs/compose.prod.yml ps

# 容器日志
docker compose --env-file /opt/app/deploy/ecs/runtime.env -f /opt/app/deploy/ecs/compose.prod.yml logs --tail=200

# Nginx
sudo nginx -t
sudo systemctl status nginx --no-pager
```
