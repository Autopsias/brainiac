<#
package_intunewin.ps1 — wrap the SIGNED one-dir bundle into a Win32 .intunewin
for upload to Intune. Run AFTER build_windows.ps1 + sign_windows.ps1.

This produces the .intunewin file only. Creating the Intune Win32 app, setting
USER install context, and enabling the Intune Management Extension as a
*Managed Installer* are portal/Graph steps gated on PW-3 (WDAC Managed-Installer
policy) and PW-4 (a managed test device) — see the runbook. Those steps are
PENDING EXTERNAL and are NOT performed by this script.

Prereq: IntuneWinAppUtil.exe (Microsoft Win32 Content Prep Tool).
#>
[CmdletBinding()]
param(
  [string]$Repo = (Resolve-Path "$PSScriptRoot\..\..\.."),
  [string]$Tool = "$PSScriptRoot\IntuneWinAppUtil.exe",
  [string]$OutDir = "$Repo\dist\intune"
)
$ErrorActionPreference = "Stop"

$staging = "$Repo\dist\intune-staging"
$bundle  = "$Repo\dist\brain"
if (-not (Test-Path "$bundle\brain.exe")) { throw "no built bundle — run build_windows.ps1 (+ sign_windows.ps1) first" }

# Stage payload (INS-01 — one install lands the full operational layer):
#   .\brain\      the one-dir engine bundle
#   .\skills\     the s08 .skill bundles (dist/cowork-skills)
#   .\routines\   the s07 task manifest
#   .\overlay\template\  the generic overlay template (first-run scaffold source)
#   install/uninstall/detect scripts + brain-init.cmd (first-run helper)
Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$staging\brain" | Out-Null
Copy-Item -Recurse "$bundle\*" "$staging\brain\"

# (d) skills payload — build if absent, then stage.
$coworkSkills = "$Repo\dist\cowork-skills"
if (-not (Test-Path "$coworkSkills\*.skill")) {
  Write-Host "[package] building .skill bundles via tools/package_clients.py"
  & python3 "$Repo\tools\package_clients.py" | Out-Null
}
if (Test-Path "$coworkSkills\*.skill") {
  New-Item -ItemType Directory -Force -Path "$staging\skills" | Out-Null
  Copy-Item "$coworkSkills\*.skill" "$staging\skills\"
} else {
  Write-Warning "[package] no .skill bundles found in $coworkSkills — skills/ will be empty"
}

# (e) task manifest.
New-Item -ItemType Directory -Force -Path "$staging\routines" | Out-Null
Copy-Item "$Repo\routines\manifest.json" "$staging\routines\manifest.json"

# (+) overlay template (first-run scaffold source) + first-run helper.
New-Item -ItemType Directory -Force -Path "$staging\overlay" | Out-Null
Copy-Item -Recurse "$Repo\overlay\template" "$staging\overlay\template"

Copy-Item "$PSScriptRoot\install.cmd","$PSScriptRoot\uninstall.cmd",`
          "$PSScriptRoot\detection.ps1","$PSScriptRoot\brain-init.cmd" $staging
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

if (-not (Test-Path $Tool)) {
  Write-Warning "IntuneWinAppUtil.exe not found at $Tool. Download the Microsoft Win32 Content Prep Tool, then re-run."
  Write-Host "Would run: $Tool -c `"$staging`" -s install.cmd -o `"$OutDir`" -q"
  exit 3
}
& $Tool -c $staging -s "install.cmd" -o $OutDir -q
Write-Host "Wrote $OutDir\install.intunewin" -ForegroundColor Green
Write-Host @"

Intune Win32 app settings to apply in the portal (PENDING PW-3/PW-4):
  Install command   : install.cmd
  Uninstall command : uninstall.cmd
  Install behavior  : User           <-- USER context (%LOCALAPPDATA%, no admin)
  Detection rule    : custom script  -> detection.ps1 (run as user, no admin)
  Return codes      : 0 = success
  Managed Installer : enable the Intune Management Extension as a Managed
                      Installer in the WDAC policy (PW-3) so anything IME
                      installs becomes WDAC-trusted automatically.
"@ -ForegroundColor Yellow
