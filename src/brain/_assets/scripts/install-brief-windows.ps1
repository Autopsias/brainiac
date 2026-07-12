# install-brief-windows.ps1 — install brain-daily-brief as a Windows Scheduled Task (UX-02)
#
# Usage (run as the same user who will run brain, NOT as Administrator):
#   .\scripts\install-brief-windows.ps1 -VaultPath C:\Users\you\brain\vault
#
# Prerequisites:
#   - brain.exe must be on PATH (test: brain --version)
#
# Uninstall:
#   Unregister-ScheduledTask -TaskName "brain-daily-brief" -Confirm:$false
#
# Threat model: the signing key lives in Windows Credential Manager (service
# profile-a-brain-audit-key) and is resolved AT RUNTIME by the brain process
# itself (audit.py resolve_signing_key(), env -> keyring fallthrough). It is
# NEVER embedded in this script, the task action, or the task XML. See
# docs/operations/s09-evidence.md § Scheduled-task threat model.
param(
    [Parameter(Mandatory=$true)]
    [string]$VaultPath,

    [string]$BrainExe   = "brain",
    [string]$TaskName   = "",
    [string]$LogDir     = "$env:USERPROFILE\.brain\logs"
)
$ErrorActionPreference = "Stop"

# Per-vault task name (mirrors brain.config.nightly_label's sha256(resolved-vault)[:8]
# slug) so two registered vaults don't install to one shared Scheduled Task and
# clobber each other's job. Pass -TaskName explicitly to override.
$LegacyTask = "brain-daily-brief"
if ([string]::IsNullOrEmpty($TaskName)) {
    $resolved = Resolve-Path -LiteralPath $VaultPath -ErrorAction SilentlyContinue
    $vpath = if ($resolved) { $resolved.Path } else { $VaultPath }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($vpath)
    $hash  = ([System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes) |
              ForEach-Object { $_.ToString('x2') }) -join ''
    $TaskName = "brain-daily-brief-" + $hash.Substring(0, 8)
}

# One-time migration off the legacy SHARED task name.
if ($TaskName -ne $LegacyTask) {
    $existing = Get-ScheduledTask -TaskName $LegacyTask -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $LegacyTask -Confirm:$false
        Write-Host "migrated: retired legacy shared task $LegacyTask (now per-vault)"
    }
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Audit signing key: provision create-if-absent (never rotates) into the
# Credential Manager via the engine; the drain resolves it at runtime, so
# nothing key-shaped goes into the task action below.
try {
    & $BrainExe audit-key
} catch {
    Write-Warning "audit-key provisioning failed: $_"
    Write-Warning "Captures will not be drained (drain fails closed, unsigned)."
    Write-Warning "Run '$BrainExe audit-key' manually later — no reinstall needed."
}

$logFile = "$LogDir\brief-$(Get-Date -Format 'yyyy-MM-dd').log"

# Task action: the `maintain` umbrella -- sync --publish + brief, PLUS the
# date-gated branches (Mon=health, Tue=integrity, Sun=digest,
# 1st=graphify-documented-only). This is THE single sanctioned OS task
# (`brain-nightly`, persistence-budget.md THE LOCK) -- see
# src/brain/core.py BrainCore.maintain. routines/manifest.json id "brain-nightly".
$scriptBlock = @"
`$env:BRAIN_VAULT = '$VaultPath'
`$log = '$LogDir\brief-' + (Get-Date -Format 'yyyy-MM-dd') + '.log'
'=== brain-daily-brief / brain-nightly ' + (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ') + ' ===' | Out-File -Append -FilePath `$log
& '$BrainExe' maintain --json 2>&1 | Out-File -Append -FilePath `$log
# Rotate logs older than 30 days
Get-ChildItem '$LogDir\brief-*.log' | Where-Object { `$_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force
"@

$action   = New-ScheduledTaskAction -Execute "powershell.exe" `
              -Argument "-NonInteractive -WindowStyle Hidden -Command `"$scriptBlock`""
# HOURLY (owner decision 2026-07-11, parity with the macOS installer):
# ingestion is frequent — every firing runs the incremental/idempotent work;
# weekly/monthly branches stay date-gated inside `brain maintain`.
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
              -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet `
              -RunOnlyIfNetworkAvailable:$false `
              -StartWhenAvailable `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Limited -Force | Out-Null

Write-Host "Installed: $TaskName (hourly umbrella)"
Write-Host "  Logs:      $LogDir\brief-YYYY-MM-DD.log"
Write-Host "  Status:    Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Dry-run:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Uninstall: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
