@echo off
REM uninstall.cmd - clean USER-context removal (Intune uninstall program).
setlocal
set "DEST=%LOCALAPPDATA%\Programs\brain"
echo [brain] Removing %DEST%...
if exist "%DEST%" rmdir /s /q "%DEST%"
REM Strip DEST from user PATH (best-effort; leaves other entries intact).
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USERPATH=%%B"
set "NEWPATH=%USERPATH:;%DEST%=%"
setx Path "%NEWPATH%" >nul 2>&1
echo [brain] Uninstall complete.
endlocal
exit /b 0
