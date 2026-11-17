@echo off
setlocal DisableDelayedExpansion
set "EDFC_UPDATER_ROOT=%~dp0"
set "EDFC_UPDATER_CHANNEL=%~1"
set "EDFC_UPDATER_REVISION=%~2"
if not "%~3"=="" (
    echo Invalid arguments: expected release TAG or main SHA.
    pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; try { $library = Join-Path $env:EDFC_UPDATER_ROOT 'tools\windows_updater.ps1'; if (-not (Test-Path -LiteralPath $library -PathType Leaf)) { $library = Join-Path $env:EDFC_UPDATER_ROOT '.update\recovery\windows_updater.ps1' }; . $library -Library; Invoke-Updater -Root $env:EDFC_UPDATER_ROOT -Channel $env:EDFC_UPDATER_CHANNEL -Revision $env:EDFC_UPDATER_REVISION; exit 0 } catch { Write-Host $_ -ForegroundColor Red; exit 1 }"
set "EDFC_UPDATER_EXIT=%ERRORLEVEL%"
if not "%EDFC_UPDATER_EXIT%"=="0" (
    echo.
    echo The updater could not complete. Review the error above.
) else (
    echo.
    echo The updater has finished.
)
pause
exit /b %EDFC_UPDATER_EXIT%
