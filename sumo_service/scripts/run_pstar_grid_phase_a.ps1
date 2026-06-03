# Phase A (수동 실행): phase1 잔여 fair 최대 3 + phase2 stress 1 run
# 지금 돌아가는 컨테이너는 멈추지 않음. -WaitForIdle 이면 active container 0 될 때까지 대기.
#
#   powershell -File sumo_service/scripts/run_pstar_grid_phase_a.ps1
#   powershell -File sumo_service/scripts/run_pstar_grid_phase_a.ps1 -NoWait   # 이미 idle일 때
#
param(
    [switch]$NoWait,
    [int]$JobsFair = 4,
    [int]$JobsStressOne = 1,
    [string]$StressRunId = "pgrid_stress55_cap49_p80",
    [double]$SimDuration = 7200,
    [string]$OutDir = ".temp/pstar_grid"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent)

$log = Join-Path $OutDir "phase_a.log"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
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
$env:N_BACKGROUND_CARS = if ($env:N_BACKGROUND_CARS) { $env:N_BACKGROUND_CARS } else { "200" }
$env:BENCH_STEP_LENGTH = if ($env:BENCH_STEP_LENGTH) { $env:BENCH_STEP_LENGTH } else { "2" }

$py = "python -u sumo_service/scripts/run_pstar_grid_sweep.py --docker --skip-cleanup --sim-duration $SimDuration --out-dir $OutDir"

if (-not $NoWait) {
    Write-Log "Phase A: waiting for active sumo containers to finish (no docker stop)..."
    while ((Get-SumoRunContainerCount) -gt 0) {
        Write-Log "  idle wait... running=$(Get-SumoRunContainerCount)"
        Start-Sleep -Seconds 45
    }
    Write-Log "idle — starting Phase A"
}

# 1) fair 잔여 (skip-ok, 최대 3 pending)
$env:DOCKER_MAX_JOBS = [string]$JobsFair
Write-Log "=== Phase A.1: batch1 fair remainder (jobs=$JobsFair) ==="
Invoke-Expression "$py --batch 1 --skip-ok --jobs $JobsFair" 2>&1 | Tee-Object -FilePath $log -Append
if ($LASTEXITCODE -ne 0) { Write-Log "Phase A.1 failed exit=$LASTEXITCODE"; exit $LASTEXITCODE }

# 2) stress 1 run (기본 cap49 P*=0.80; 다른 run이면 -StressRunId)
$env:DOCKER_MAX_JOBS = [string]$JobsStressOne
Write-Log "=== Phase A.2: stress 1 run $StressRunId (jobs=$JobsStressOne) ==="
Invoke-Expression "$py --run-ids $StressRunId --skip-ok --jobs $JobsStressOne" 2>&1 | Tee-Object -FilePath $log -Append
Write-Log "Phase A done exit=$LASTEXITCODE"
exit $LASTEXITCODE
