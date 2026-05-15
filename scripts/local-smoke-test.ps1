param(
  [string]$EnvFilePath = "",
  [string]$DocSyncProfiles = "",
  [switch]$RunDocSync
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$ComposeFile = Join-Path $RepoRoot "local/docker-compose.local.yml"
$EnvFile = if ($EnvFilePath) { (Resolve-Path -LiteralPath $EnvFilePath).Path } else { Join-Path $RepoRoot "local/.env.local" }
$EnvExample = Join-Path $RepoRoot "local/.env.local.example"
$ComposeArgs = @("compose", "--env-file", $EnvFile, "-f", $ComposeFile)

function Write-Step {
  param([string]$Message)
  Write-Host ""
  Write-Host "[local-smoke] $Message"
}

function Show-RecentLogs {
  Write-Host ""
  Write-Host "[local-smoke] Recent container logs:"
  & docker @ComposeArgs logs --tail=120
}

function Fail-WithLogs {
  param([string]$Message)
  Write-Host ""
  Write-Host "[local-smoke] FAILED: $Message" -ForegroundColor Red
  try { Show-RecentLogs } catch { Write-Host "[local-smoke] Could not read logs: $($_.Exception.Message)" }
  Write-Host ""
  Write-Host "[local-smoke] To clean up manually, run:"
  Write-Host "docker compose -f local/docker-compose.local.yml down"
  exit 1
}

function Test-Http {
  param(
    [string]$Url,
    [bool]$Required = $true
  )

  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 5
    if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
      Write-Host "[local-smoke] OK $Url -> HTTP $($response.StatusCode)"
      return $true
    }
    if ($Required) {
      Fail-WithLogs "$Url returned HTTP $($response.StatusCode)"
    }
    Write-Host "[local-smoke] Optional check failed: $Url -> HTTP $($response.StatusCode)"
    return $false
  } catch {
    if ($Required) {
      Fail-WithLogs "$Url request failed: $($_.Exception.Message)"
    }
    Write-Host "[local-smoke] Optional check failed: $Url is not available."
    return $false
  }
}

function Get-LocalEnvValue {
  param(
    [string]$Name,
    [string]$Default
  )

  if (!(Test-Path -LiteralPath $EnvFile)) {
    return $Default
  }

  $line = Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -Last 1
  if (!$line) {
    return $Default
  }
  return (($line -split "=", 2)[1]).Trim().Trim('"').Trim("'")
}

function Wait-Postgres {
  $pgUser = Get-LocalEnvValue "POSTGRES_USER" "app"
  $pgDb = Get-LocalEnvValue "POSTGRES_DB" "app"

  for ($i = 1; $i -le 60; $i++) {
    & docker @ComposeArgs exec -T postgres pg_isready -U $pgUser -d $pgDb | Out-Null
    if ($LASTEXITCODE -eq 0) {
      return
    }
    Start-Sleep -Seconds 2
  }

  Fail-WithLogs "postgres did not become ready in time"
}

function Apply-LocalMigrations {
  $pgUser = Get-LocalEnvValue "POSTGRES_USER" "app"
  $pgDb = Get-LocalEnvValue "POSTGRES_DB" "app"
  $migrationDir = Join-Path $RepoRoot "db/migrations"

  Write-Step "Applying local SQL migrations"
  Get-ChildItem -LiteralPath $migrationDir -Filter "*.sql" | Sort-Object Name | ForEach-Object {
    Write-Host "[local-smoke] Applying $($_.Name)"
    $containerPath = "/tmp/aliecs-migration-$($_.Name)"
    & docker @ComposeArgs cp $_.FullName "postgres:$containerPath"
    if ($LASTEXITCODE -ne 0) {
      Fail-WithLogs "copy migration failed: $($_.Name)"
    }
    & docker @ComposeArgs exec -T postgres psql -U $pgUser -d $pgDb -v ON_ERROR_STOP=1 -f $containerPath
    if ($LASTEXITCODE -ne 0) {
      Fail-WithLogs "migration failed: $($_.Name)"
    }
  }
}

function Test-EnvNameExists {
  param([string]$Pattern)
  if (!(Test-Path -LiteralPath $EnvFile)) {
    return $false
  }
  $match = Select-String -LiteralPath $EnvFile -Pattern $Pattern -ErrorAction SilentlyContinue | Select-Object -First 1
  return [bool]$match
}

function Test-DocSyncConfig {
  $hasProfile = Test-EnvNameExists "^\s*WECOM_ENV_PROFILES\s*="
  $hasCorp = Test-EnvNameExists "^\s*WECOM_.*_CORP_ID\s*="
  $hasSecret = Test-EnvNameExists "^\s*WECOM_.*_APP_SECRET"
  $hasDoc = (Test-EnvNameExists "^\s*WEDOC_.*_DOCID\s*=") -or (Test-EnvNameExists "^\s*SMARTSHEET_.*_ID\s*=")
  return (($hasProfile -or ($hasCorp -and $hasSecret)) -and $hasCorp -and $hasSecret -and $hasDoc)
}

if (!(Test-Path -LiteralPath $EnvFile)) {
  Write-Host "[local-smoke] Missing env file: $EnvFile"
  Write-Host "[local-smoke] Copy the local example and keep only local test values:"
  Write-Host "Copy-Item local/.env.local.example local/.env.local"
  Write-Host "[local-smoke] Example file: $EnvExample"
  exit 1
}

Write-Step "Checking Docker Compose config"
& docker @ComposeArgs config | Out-Null
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Step "Starting local services"
& docker @ComposeArgs up --build -d
if ($LASTEXITCODE -ne 0) { Fail-WithLogs "docker compose up failed" }

Write-Step "Waiting for postgres"
Wait-Postgres
Apply-LocalMigrations

Write-Step "Waiting for backend-api health"
$healthy = $false
for ($i = 1; $i -le 60; $i++) {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8000/healthz" -TimeoutSec 3
    if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
      $healthy = $true
      break
    }
  } catch {
    Start-Sleep -Seconds 2
  }
}

if (!$healthy) {
  Fail-WithLogs "backend-api did not pass /healthz in time"
}

Write-Step "Checking local entrypoints"
Test-Http "http://localhost:8080" $true | Out-Null
Test-Http "http://localhost:8081" $true | Out-Null
Test-Http "http://localhost:8000/healthz" $true | Out-Null
Test-Http "http://localhost:8000/api/healthz" $false | Out-Null

if ($RunDocSync) {
  Write-Step "Checking doc-sync-worker command"
  if (Test-DocSyncConfig) {
    $syncArgs = @("run", "--rm", "doc-sync-worker", "python", "-m", "app.main", "sync-wecom-full")
    if ($DocSyncProfiles) {
      $syncArgs += @("--profiles", $DocSyncProfiles)
    }
    & docker @ComposeArgs @syncArgs
    if ($LASTEXITCODE -ne 0) { Fail-WithLogs "doc-sync-worker real sync failed" }
  } else {
    Write-Host "[local-smoke] Skipping real WeCom sync: env file does not contain WECOM profiles, corp id, app secret and docid variable names."
    Write-Host "[local-smoke] To run real sync, fill local/.env.local or pass -EnvFilePath with a file containing WECOM_* and WEDOC_/SMARTSHEET_* variables."
  }
} else {
  Write-Step "Checking doc-sync-worker help"
  & docker @ComposeArgs run --rm doc-sync-worker python -m app.main --help
  if ($LASTEXITCODE -ne 0) { Fail-WithLogs "doc-sync-worker help failed" }
}

Write-Step "Container status"
& docker @ComposeArgs ps
if ($LASTEXITCODE -ne 0) { Fail-WithLogs "docker compose ps failed" }

Write-Host ""
Write-Host "[local-smoke] Local smoke test finished."
Write-Host "[local-smoke] To clean up manually, run:"
Write-Host "docker compose -f local/docker-compose.local.yml down"
