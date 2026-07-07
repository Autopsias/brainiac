@echo off
REM brain-init.cmd - FIRST-RUN completion of a brain engine install (INS-02).
REM
REM install.cmd (the Intune install program) lands the ENGINE + the SKILL payload
REM + the task MANIFEST into %LOCALAPPDATA%\Programs\brain. Machine provisioning
REM happens before the user has a vault, so overlay personalization + scheduled-
REM task registration are deferred to this per-user, once-per-vault command.
REM
REM Usage (run as the user, NO admin), pointing at your vault:
REM   "%LOCALAPPDATA%\Programs\brain\brain-init.cmd" C:\Users\you\brain\vault
REM
REM It runs `brain init --full` (host client) which:
REM   - scaffolds + validates the overlay/{voice,brand,keywords,people}/ layer,
REM   - drives the ONE sanctioned Windows scheduled task registration.
REM
REM Task registration: the bundled brain.exe ships WITHOUT scripts/, so the host
REM leg reports the exact Task Scheduler install command to run (scripts/
REM install-brief-windows.ps1). To register the daily task directly, run that
REM PowerShell installer with your vault path (see the printed hint / the Intune
REM runbook docs/operations/packaging-windows-runbook.md).
setlocal
set "DEST=%LOCALAPPDATA%\Programs\brain"
set "VAULT=%~1"

if "%VAULT%"=="" (
  echo Usage: brain-init.cmd ^<path-to-vault^>
  echo   e.g. "%DEST%\brain-init.cmd" %USERPROFILE%\brain\vault
  exit /b 2
)
if not exist "%DEST%\brain.exe" (
  echo [brain] ERROR: brain.exe not found in %DEST% - run the Intune install first.
  exit /b 3
)

set "BRAIN_VAULT=%VAULT%"
REM Point the manifest + template at the landed copies so init works with no repo.
set "BRAIN_ROUTINES_MANIFEST=%DEST%\routines\manifest.json"

echo [brain] Running first-run init against vault: %VAULT%
"%DEST%\brain.exe" --vault "%VAULT%" init --full ^
  --overlay-dir "%VAULT%\overlay" ^
  --template-dir "%DEST%\overlay\template" ^
  --manifest "%DEST%\routines\manifest.json" ^
  --save-cowork-prompt "%VAULT%\.brain\routines\cowork-registrar-prompt.md"
set "RC=%ERRORLEVEL%"

echo.
echo [brain] init exit code: %RC%
echo [brain] To register the daily maintenance task (once), run in PowerShell:
echo   scripts\install-brief-windows.ps1 -VaultPath "%VAULT%"
echo   (ships in the repo; see docs\operations\packaging-windows-runbook.md)
endlocal & exit /b %RC%
