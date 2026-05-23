param(
    [string]$EnvFile = "C:\tmp\aliecs-release-meta.env",
    [string]$SecretName = "aliecs/prod/release-meta",
    [string]$RegionId = "cn-hangzhou",
    [switch]$CreateIfMissing
)

$ErrorActionPreference = "Stop"

function Require-Command($Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing command: $Name. Install and configure Alibaba Cloud CLI first."
    }
}

Require-Command "aliyun"

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Env file not found: $EnvFile"
}

$bytes = [System.IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $EnvFile))
$secretData = [Convert]::ToBase64String($bytes)

if ([Text.Encoding]::UTF8.GetByteCount($secretData) -gt 30720) {
    throw "KMS SecretData limit is 30KB. Current base64 payload is too large."
}

$versionId = "v" + (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")

$describeOutput = & aliyun kms DescribeSecret `
    --RegionId $RegionId `
    --SecretName $SecretName `
    --output json 2>$null

if ($LASTEXITCODE -eq 0) {
    & aliyun kms PutSecretValue `
        --RegionId $RegionId `
        --SecretName $SecretName `
        --VersionId $versionId `
        --SecretData $secretData `
        --SecretDataType text `
        --output json | Out-Null
} elseif ($CreateIfMissing) {
    & aliyun kms CreateSecret `
        --RegionId $RegionId `
        --SecretName $SecretName `
        --SecretType Generic `
        --VersionId $versionId `
        --SecretData $secretData `
        --SecretDataType text `
        --Description "AliECS release-meta.env base64 payload" `
        --output json | Out-Null
} else {
    throw "KMS secret does not exist: $SecretName. Re-run with -CreateIfMissing after confirming the name."
}

if ($LASTEXITCODE -ne 0) {
    throw "Failed to upload env payload to KMS secret: $SecretName"
}

Write-Host "Uploaded env payload to KMS secret '$SecretName' in region '$RegionId' with version '$versionId'."
