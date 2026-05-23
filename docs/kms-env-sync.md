# KMS env sync

AliECS can keep deployment environment variables outside GitHub and ECS disk state:

1. Keep one private local file, for example `C:\tmp\aliecs-release-meta.env`.
2. Upload that file to Alibaba Cloud KMS Secrets Manager as a Generic Secret.
3. During deployment, ECS fetches the latest secret and writes `/root/AliECS/deploy/ecs/release-meta.env`.
4. `deploy/ecs/deploy.sh` generates `runtime.env` from `release-meta.env` as before.

## Local upload

Install and configure Alibaba Cloud CLI locally, then run:

```powershell
.\scripts\sync-release-env-to-kms.ps1 `
  -EnvFile C:\tmp\aliecs-release-meta.env `
  -SecretName aliecs/prod/release-meta `
  -RegionId cn-hangzhou `
  -CreateIfMissing
```

After the secret exists, regular updates can omit `-CreateIfMissing`.

## ECS read path

Install Alibaba Cloud CLI on ECS and bind a RAM Role with read-only permission for the chosen KMS secret.

Deployment uses:

```bash
ALIYUN_KMS_SECRET_NAME=aliecs/prod/release-meta \
ALIYUN_REGION_ID=cn-hangzhou \
bash /root/AliECS/deploy/ecs/fetch-release-env.sh
```

If `ALIYUN_KMS_SECRET_NAME` is not set, deployment keeps the existing ECS-local `release-meta.env`. This is the safe transition mode before KMS and ECS RAM Role are ready.

The script does not print secret values. It only writes:

```text
/root/AliECS/deploy/ecs/release-meta.env
```

Then the existing deployment flow continues.

## Required KMS permissions

The local uploader needs permission to create or update the secret:

- `kms:DescribeSecret`
- `kms:CreateSecret`
- `kms:PutSecretValue`

The ECS RAM Role only needs read permission:

- `kms:GetSecretValue`

If the KMS secret uses a customer-managed encryption key, grant the caller the required decrypt or data-key permission for that key.

## Safety rules

- Do not upload `local/.env.local` directly.
- Use a dedicated deployment file based on `deploy/ecs/release-meta.env.example`.
- Do not commit real `release-meta.env`, `runtime.env`, AccessKey, token, or app secret.
- Use separate secret names for different environments, such as `aliecs/prod/release-meta` and `aliecs/staging/release-meta`.
