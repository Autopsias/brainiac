<#
sign_windows.ps1 — Authenticode-sign the one-dir bundle with Azure Trusted
Signing (formerly "Azure Code Signing" / "Trusted Signing"), with an RFC-3161
timestamp.  >>> PENDING EXTERNAL: PW-2 (Azure Trusted Signing onboarding) <<<

WHY Azure Trusted Signing and NOT an EV cert (consensus HARDENED note):
  - The load-bearing trust control on a managed Example Corp endpoint is the Intune
    Managed Installer / WDAC trust (see intune\), NOT SmartScreen reputation.
  - EV certs NO LONGER auto-bypass SmartScreen in 2026 — buying one is pointless
    here. Azure Trusted Signing (~$10/mo) gives a valid Authenticode signature +
    RFC-3161 timestamp, which is all WDAC/Defender need.

REGION GATING (HARDENED:claude — verify BEFORE booking):
  Azure Trusted Signing Public Trust identities are only issuable from accounts
  whose billing region is US / CA / EU / UK. Confirm the Example Corp tenant's region is
  eligible during PW-2 onboarding; if not, the contingency is a second signing
  identity in an eligible tenant (see runbook "Contingency").

MAR-2026 INTERMEDIATE-CA MIGRATION CONTINGENCY (consensus HARDENED note):
  In Mar-2026 the Azure Trusted Signing intermediate CA rotated; some valid
  signatures briefly raised SmartScreen warnings until the new chain seeded.
  Because we rely on Managed-Installer/WDAC trust (not SmartScreen) this does NOT
  block managed-device install — but document it, and the contingency is a
  reputation wait-out or a second signing identity. See the runbook.

PREREQUISITES (all PENDING until PW-2 completes):
  - Azure account onboarded to Trusted Signing; a Trusted Signing Account +
    Certificate Profile created; the build identity granted the
    "Trusted Signing Certificate Profile Signer" role.
  - signtool.exe (Windows SDK) + the Trusted Signing dlib (Azure.CodeSigning.Dlib)
  - An Azure.CodeSigning metadata json (endpoint + account + profile).
#>
[CmdletBinding()]
param(
  [string]$Repo = (Resolve-Path "$PSScriptRoot\..\.."),
  [Parameter(Mandatory=$true)][string]$MetadataJson,   # Azure.CodeSigning metadata
  [string]$DlibPath = "$env:ProgramFiles\Azure\Azure.CodeSigning.Dlib\bin\x64\Azure.CodeSigning.Dlib.dll",
  [string]$TimestampUrl = "http://timestamp.acs.microsoft.com",
  [string]$SignTool = "${env:ProgramFiles(x86)}\Windows Kits\10\bin\x64\signtool.exe"
)
$ErrorActionPreference = "Stop"

$dist = "$Repo\dist\brain"
if (-not (Test-Path "$dist\brain.exe")) { throw "no built bundle at $dist — run build_windows.ps1 first" }

# Sign EVERY PE in the one-dir bundle (the exe + bundled .dll/.pyd), so WDAC's
# per-file signature check passes on all of them, and timestamp each so the
# signature outlives the cert validity window.
$pes = Get-ChildItem -Path $dist -Recurse -Include *.exe,*.dll,*.pyd
Write-Host "Signing $($pes.Count) PE files via Azure Trusted Signing (RFC-3161 timestamp)..." -ForegroundColor Cyan
foreach ($pe in $pes) {
  & $SignTool sign /v /debug /fd SHA256 `
    /tr $TimestampUrl /td SHA256 `
    /dlib $DlibPath /dmdf $MetadataJson `
    $pe.FullName
  if ($LASTEXITCODE -ne 0) { throw "signtool failed on $($pe.FullName)" }
}

# Verify + emit the SIGNED hash list (this is the hash list that goes in the
# approval evidence pack — see _evidence/s07/sha256-artifacts.txt for the unsigned set).
& $SignTool verify /pa /v "$dist\brain.exe"
Get-ChildItem $dist -Recurse -Include *.exe,*.dll,*.pyd |
  Get-FileHash -Algorithm SHA256 |
  Format-Table Hash, Path -AutoSize
Write-Host "Signed + timestamped. Next: intune\package_intunewin.ps1" -ForegroundColor Green
