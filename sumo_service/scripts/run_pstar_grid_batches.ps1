# P* grid: batch 1 (fair) then batch 2 (stress). Logs to .temp/pstar_grid/batch_run.log
param(
    [int]$JobsFirst = 3,
    [int]$JobsRest = 5
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent)

$logDir = ".temp/pstar_grid"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "batch_run.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $log -Value $line
    Write-Host $line
}

if (-not $env:PREDICTION_API_KEY) {
    $cid = (docker ps -q --filter "name=backend-sumo-service-run" 2>$null | Select-Object -First 1)
    if ($cid) {
        $pairs = docker inspect $cid --format '{{range .Config.Env}}{{println .}}{{end}}' 2>$null
        foreach ($p in $pairs) {
            if ($p -match '^PREDICTION_API_KEY=(.+)$') {
                $env:PREDICTION_API_KEY = $matches[1]
                Write-Log "PREDICTION_API_KEY loaded from container $cid"
                break
            }
        }
    }
}
if (-not $env:PREDICTION_API_KEY) {
    Write-Log "ERROR: set PREDICTION_API_KEY before running"
    exit 1
}

$env:EXPERIMENT_FAST = "1"
if (-not $env:DOCKER_MAX_JOBS) { $env:DOCKER_MAX_JOBS = [string]$JobsFirst }
if (-not $env:N_BACKGROUND_CARS) { $env:N_BACKGROUND_CARS = "200" }
if (-not $env:BENCH_STEP_LENGTH) { $env:BENCH_STEP_LENGTH = "2" }

$py = "python -u sumo_service/scripts/run_pstar_grid_sweep.py --docker --skip-cleanup --sim-duration 7200 --out-dir .temp/pstar_grid"

Write-Log "=== batch 1 (fair, 6 runs) jobs=$JobsFirst ==="
Invoke-Expression "$py --jobs $JobsFirst --batch 1" 2>&1 | Tee-Object -FilePath $log -Append
if ($LASTEXITCODE -ne 0) {
    Write-Log "batch 1 failed exit=$LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Log "=== batch 2 (stress, 6 runs) jobs=$JobsRest ==="
Invoke-Expression "$py --jobs $JobsRest --batch 2 --skip-ok" 2>&1 | Tee-Object -FilePath $log -Append
Write-Log "done exit=$LASTEXITCODE"
exit $LASTEXITCODE
