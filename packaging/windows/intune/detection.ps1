<#
detection.ps1 — Intune Win32 app custom detection rule.
Exit 0 + STDOUT  => app detected (installed). Exit 0 + no output => not detected.
Keys on the per-user install stamp written by install.cmd. USER-context safe
(reads %LOCALAPPDATA% of the running user — Intune evaluates this in user context).
#>
$ver = "0.2.0"
$stamp = Join-Path $env:LOCALAPPDATA "Programs\brain\.version"
$exe   = Join-Path $env:LOCALAPPDATA "Programs\brain\brain.exe"
if ((Test-Path $exe) -and (Test-Path $stamp) -and ((Get-Content $stamp -Raw).Trim() -eq $ver)) {
  Write-Output "brain $ver detected"
  exit 0
}
exit 0
