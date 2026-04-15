# Ubuntu 24.04（`ubuntu_24_04_x64_20G_alibase_20260213.vhd`）部署指南

> 仓库地址：`https://github.com/huozao/AliECS`

## 一、ECS 首次准备

### 1）安装 Docker 与插件
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
sudo usermod -aG docker $USER
newgrp docker
```

### 2）部署目录
```bash
sudo mkdir -p /opt/app
sudo chown -R $USER:$USER /opt/app
cd /opt/app
```

### 3）拉取仓库
```bash
git clone https://github.com/huozao/AliECS .
```

### 4）修改部署配置
编辑 `deploy/ecs/release-meta.env`：
- `GHCR_BASE=ghcr.io/huozao`
- `POSTGRES_PASSWORD` 改成强密码
- `DATABASE_URL` 与密码保持一致

## 二、本地/服务器手工验证

### 1）先做语法检查
```bash
python3 -m py_compile services/backend-api/app/main.py sync-pipeline/main_flow.py
bash -n deploy/ecs/deploy.sh deploy/ecs/migrate.sh deploy/ecs/healthcheck.sh deploy/ecs/rollback.sh
```

### 2）本地（或服务器）直接起服务验证
```bash
docker compose -f local/docker-compose.local.yml up --build -d
curl -fsS http://127.0.0.1:8000/healthz
```

## 三、接入 GitHub Actions 自动发布

### 1）仓库 Secrets
在 GitHub 仓库配置：
- `ECS_HOST`
- `ECS_USER`
- `ECS_SSH_KEY`

### 2）打标签触发发布
```bash
git tag v0.1.0
git push origin v0.1.0
```

## 四、ECS 侧实际发布与回滚

### 发布
```bash
cd /opt/app
./deploy/ecs/deploy.sh v0.1.0
```

### 手动健康检查
```bash
./deploy/ecs/healthcheck.sh
```

### 手动回滚
```bash
./deploy/ecs/rollback.sh
```
