@echo off
REM install.cmd - USER-context install of the `brain` one-dir bundle to
REM %LOCALAPPDATA% (NO admin, NO Program Files). This is the command Intune runs
REM as the install program for the Win32 (.intunewin) app.
REM
REM ASR/CFA compliance (PKG-02): the install target is %LOCALAPPDATA%, never
REM Documents/OneDrive (Controlled Folder Access protects those). The payload is
REM copied as-is; nothing is unpacked at runtime and no child interpreter is
REM spawned. See docs/operations/asr-cfa-design-rules.md.
setlocal
set "DEST=%LOCALAPPDATA%\Programs\brain"

echo [brain] Installing to %DEST% (user context, no admin)...
if not exist "%DEST%" mkdir "%DEST%"

REM Robocopy mirrors the one-dir payload (this script ships alongside the bundle).
robocopy "%~dp0brain" "%DEST%" /MIR /NJH /NJS /NDL /NP
REM Robocopy exit codes 0-7 are success (8+ is error).
if %ERRORLEVEL% GEQ 8 (
  echo [brain] ERROR: robocopy failed with %ERRORLEVEL%
  exit /b %ERRORLEVEL%
)

REM Per-user PATH (HKCU, no admin) so `brain` resolves in any user shell.
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USERPATH=%%B"
echo %USERPATH% | find /i "%DEST%" >nul
if errorlevel 1 (
  setx Path "%USERPATH%;%DEST%" >nul
  echo [brain] Added %DEST% to user PATH.
)

REM Detection stamp (Intune detection rule keys on this; see detection.ps1).
echo 0.2.0> "%DEST%\.version"
echo [brain] Install complete.
endlocal
exit /b 0
