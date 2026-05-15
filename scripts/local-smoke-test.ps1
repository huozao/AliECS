param()

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$ComposeFile = Join-Path $RepoRoot "local/docker-compose.local.yml"
$EnvFile = Join-Path $RepoRoot "local/.env.local"
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

if (!(Test-Path -LiteralPath $EnvFile)) {
  Write-Host "[local-smoke] Missing local/.env.local."
  Write-Host "[local-smoke] Copy the local example and keep only local test values:"
  Write-Host "Copy-Item local/.env.local.example local/.env.local"
  Write-Host "[local-smoke] Example file: $EnvExample"
  exit 1
}

Write-Step "Checking Docker Compose config"
& docker @ComposeArgs config
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Step "Starting local services"
& docker @ComposeArgs up --build -d
if ($LASTEXITCODE -ne 0) { Fail-WithLogs "docker compose up failed" }

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

Write-Step "Container status"
& docker @ComposeArgs ps
if ($LASTEXITCODE -ne 0) { Fail-WithLogs "docker compose ps failed" }

Write-Host ""
Write-Host "[local-smoke] Local smoke test finished."
Write-Host "[local-smoke] To clean up manually, run:"
Write-Host "docker compose -f local/docker-compose.local.yml down"
