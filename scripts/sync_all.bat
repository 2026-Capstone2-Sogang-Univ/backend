@echo off
setlocal

set ROOT=%~dp0..
set SERVICES=sumo_service dispatch_service prediction_service
set FAILED=0

for %%S in (%SERVICES%) do (
    if exist "%ROOT%\%%S\pyproject.toml" (
        echo [sync] %%S
        uv sync --project "%ROOT%\%%S"
        if errorlevel 1 (
            echo [error] %%S sync failed
            set FAILED=1
        )
    ) else (
        echo [skip] %%S ^(pyproject.toml not found^)
    )
)

if %FAILED%==1 (
    echo.
    echo One or more services failed to sync.
    exit /b 1
)

echo.
echo All services synced successfully.
endlocal
