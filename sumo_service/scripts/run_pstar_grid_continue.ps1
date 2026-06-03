# [레거시] 자동 이어하기 — 대신 수동 2단계 사용 권장:
#   run_pstar_grid_phase_a.ps1  (fair 잔여 3 + stress 1)
#   run_pstar_grid_phase_b.ps1  (stress 잔여 5)
#
# Continue P* grid without stopping in-flight Docker runs.
# Usage: powershell -File sumo_service/scripts/run_pstar_grid_continue.ps1 -Jobs 5
#
param(
    [int]$Jobs = 5,
    [double]$SimDuration = 7200,
    [string]$OutDir = ".temp/pstar_grid"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent)

$logDir = $OutDir
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "continue_run.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

function Get-PstarOkCount {
    $n = 0
    Get-ChildItem (Join-Path $OutDir "pgrid_*.json") -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $j = Get-Content $_.FullName -Raw | ConvertFrom-Json
            if ($j.status -eq "ok") { $n++ }
        } catch { }
    }
    return $n
}

function Get-PstarOrchestratorPid {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^python' -and
            $_.CommandLine -match 'run_pstar_grid_sweep\.py'
        } |
        Select-Object -ExpandProperty ProcessId -First 1
}

function Get-SumoRunContainerCount {
    @(docker ps -q --filter "name=backend-sumo-service-run" 2>$null).Count
}

if (-not $env:PREDICTION_API_KEY) {
    $cid = (docker ps -q --filter "name=backend-sumo-service-run" 2>$null | Select-Object -First 1)
    if ($cid) {
        foreach ($p in (docker inspect $cid --format '{{range .Config.Env}}{{println .}}{{end}}' 2>$null)) {
            if ($p -match '^PREDICTION_API_KEY=(.+)$') {
                $env:PREDICTION_API_KEY = $matches[1]
                Write-Log "PREDICTION_API_KEY loaded from container $cid"
                break
            }
        }
    }
}
if (-not $env:PREDICTION_API_KEY) {
    Write-Log "ERROR: set PREDICTION_API_KEY"
    exit 1
}

$env:EXPERIMENT_FAST = "1"
$env:DOCKER_MAX_JOBS = [string][Math]::Min(6, [Math]::Max(1, $Jobs))
if (-not $env:N_BACKGROUND_CARS) { $env:N_BACKGROUND_CARS = "200" }
if (-not $env:BENCH_STEP_LENGTH) { $env:BENCH_STEP_LENGTH = "2" }

$pyBase = "python -u sumo_service/scripts/run_pstar_grid_sweep.py --docker --skip-cleanup --sim-duration $SimDuration --jobs $Jobs --out-dir $OutDir"

Write-Log "continue: jobs=$Jobs (DOCKER_MAX_JOBS=$($env:DOCKER_MAX_JOBS)), never stop in-flight containers"

# --- Phase 0: wait for currently running containers (do NOT docker stop) ---
$initialRunning = Get-SumoRunContainerCount
Write-Log "phase 0: waiting for $initialRunning active sumo run container(s) to finish on their own..."

while ((Get-SumoRunContainerCount) -gt 0) {
    $ok = Get-PstarOkCount
    $running = Get-SumoRunContainerCount
    Write-Log "  waiting... running=$running ok_json=$ok/12"
    Start-Sleep -Seconds 45
}

Write-Log "phase 0 done: no active sumo run containers"

# Stop host orchestrator only (so it does not start another jobs=3 wave). Never docker stop.
$orchPid = Get-PstarOrchestratorPid
if ($orchPid) {
    Write-Log "stopping host orchestrator PID=$orchPid only (Docker runs untouched)"
    Stop-Process -Id $orchPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

# --- Phase 1 remainder: fair batch (up to 3 left) ---
$okBefore = Get-PstarOkCount
Write-Log "=== phase 1: batch 1 fair (pending after first wave), jobs=$Jobs ==="
Invoke-Expression "$pyBase --batch 1 --skip-ok" 2>&1 | Tee-Object -FilePath $log -Append
if ($LASTEXITCODE -ne 0) {
    Write-Log "phase 1 exit=$LASTEXITCODE (see log)"
    exit $LASTEXITCODE
}
Write-Log "phase 1 done ok=$(Get-PstarOkCount)/12 (was $okBefore)"

# --- Phase 2: stress batch (6 runs) ---
Write-Log "=== phase 2: batch 2 stress, jobs=$Jobs ==="
Invoke-Expression "$pyBase --batch 2 --skip-ok" 2>&1 | Tee-Object -FilePath $log -Append
$finalOk = Get-PstarOkCount
Write-Log "all done ok=$finalOk/12 exit=$LASTEXITCODE"
exit $LASTEXITCODE
