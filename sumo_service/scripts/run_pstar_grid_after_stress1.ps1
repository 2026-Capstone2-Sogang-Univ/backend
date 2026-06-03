# Wait for Phase A stress-1 run JSON ok, then Phase B (stress remainder 5, jobs=5).
# Does not docker-stop in-flight containers.
param(
    [string]$StressRunId = "pgrid_stress55_cap49_p80",
    [int]$Jobs = 5,
    [string]$OutDir = ".temp/pstar_grid"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent)

$log = Join-Path $OutDir "after_stress1.log"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

if (-not $env:PREDICTION_API_KEY) {
    $cid = (docker ps -q --filter "name=backend-sumo-service-run" 2>$null | Select-Object -First 1)
    if ($cid) {
        foreach ($p in (docker inspect $cid --format '{{range .Config.Env}}{{println .}}{{end}}' 2>$null)) {
            if ($p -match '^PREDICTION_API_KEY=(.+)$') { $env:PREDICTION_API_KEY = $matches[1]; break }
        }
    }
}
if (-not $env:PREDICTION_API_KEY) { Write-Log "ERROR: PREDICTION_API_KEY"; exit 1 }

$jsonPath = Join-Path $OutDir "$StressRunId.json"
Write-Log "waiting for $StressRunId.json status=ok ..."
while ($true) {
    if (Test-Path $jsonPath) {
        try {
            $j = Get-Content $jsonPath -Raw | ConvertFrom-Json
            if ($j.status -eq "ok") {
                Write-Log "$StressRunId ok — starting Phase B (batch2 remainder, jobs=$Jobs)"
                break
            }
            if ($j.status -eq "error") {
                Write-Log "ERROR: $StressRunId failed: $($j.reason)"
                exit 1
            }
        } catch { }
    }
    Start-Sleep -Seconds 60
}

& "$PSScriptRoot/run_pstar_grid_phase_b.ps1" -NoWait -Jobs $Jobs -OutDir $OutDir 2>&1 | Tee-Object -FilePath $log -Append
exit $LASTEXITCODE
