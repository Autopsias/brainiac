@echo off
REM install.cmd - USER-context install of the `brain` one-dir bundle to
REM %LOCALAPPDATA% (NO admin, NO Program Files). This is the command Intune runs
REM as the install program for the Win32 (.intunewin) app.
REM
REM ONE install flow lands the full operational layer (INS-01):
REM   (a) the engine    -> %DEST%\           (one-dir brain.exe bundle)
REM   (d) the SKILLS    -> %DEST%\skills\    (s08 .skill bundles)
REM   (e) the MANIFEST  -> %DEST%\routines\  (s07 task manifest)
REM   (+) overlay template + brain-init.cmd  (first-run overlay + task reg, INS-02)
REM Overlay personalization + scheduled-task registration are deferred to
REM brain-init.cmd (run once per vault, per user) because machine provisioning
REM runs before the user has a vault. See brain-init.cmd + the runbook.
REM
REM ASR/CFA compliance (PKG-02): the install target is %LOCALAPPDATA%, never
REM Documents/OneDrive (Controlled Folder Access protects those). The payload is
REM copied as-is; nothing is unpacked at runtime and no child interpreter is
REM spawned. See docs/operations/asr-cfa-design-rules.md.
setlocal
set "DEST=%LOCALAPPDATA%\Programs\brain"

echo [brain] Installing to %DEST% (user context, no admin)...
if not exist "%DEST%" mkdir "%DEST%"

REM (a) Robocopy mirrors the one-dir engine payload (ships alongside this script).
robocopy "%~dp0brain" "%DEST%" /MIR /NJH /NJS /NDL /NP
REM Robocopy exit codes 0-7 are success (8+ is error).
if %ERRORLEVEL% GEQ 8 (
  echo [brain] ERROR: robocopy failed with %ERRORLEVEL%
  exit /b %ERRORLEVEL%
)

REM (d) SKILL payload (s08) — land the .skill bundles for the Cowork Save-skill flow.
if exist "%~dp0skills" (
  echo [brain] Landing skill bundles -> %DEST%\skills
  robocopy "%~dp0skills" "%DEST%\skills" /MIR /NJH /NJS /NDL /NP
  if %ERRORLEVEL% GEQ 8 ( echo [brain] ERROR: skills copy failed & exit /b %ERRORLEVEL% )
)

REM (e) task MANIFEST (s07) — the host/VM-aware scheduled-task manifest brain init consumes.
if exist "%~dp0routines" (
  echo [brain] Landing task manifest -> %DEST%\routines
  robocopy "%~dp0routines" "%DEST%\routines" /MIR /NJH /NJS /NDL /NP
  if %ERRORLEVEL% GEQ 8 ( echo [brain] ERROR: routines copy failed & exit /b %ERRORLEVEL% )
)

REM (+) overlay template + first-run helper (INS-02).
if exist "%~dp0overlay" (
  robocopy "%~dp0overlay" "%DEST%\overlay" /MIR /NJH /NJS /NDL /NP
)
if exist "%~dp0brain-init.cmd" copy /y "%~dp0brain-init.cmd" "%DEST%\brain-init.cmd" >nul

REM Per-user PATH (HKCU, no admin) so `brain` resolves in any user shell.
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USERPATH=%%B"
echo %USERPATH% | find /i "%DEST%" >nul
if errorlevel 1 (
  setx Path "%USERPATH%;%DEST%" >nul
  echo [brain] Added %DEST% to user PATH.
)

REM Detection stamp (Intune detection rule keys on this; see detection.ps1).
echo 0.2.0> "%DEST%\.version"
echo [brain] Install complete (engine + skills + task manifest).
echo [brain] First run (once, per vault): "%DEST%\brain-init.cmd" ^<path-to-vault^>
echo [brain]   personalizes the overlay + drives scheduled-task registration.
endlocal
exit /b 0
