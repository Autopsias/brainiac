# install-brief-windows.ps1 — install brain-daily-brief as a Windows Scheduled Task (UX-02)
#
# Usage (run as the same user who will run brain, NOT as Administrator):
#   .\scripts\install-brief-windows.ps1 -VaultPath C:\Users\you\brain\vault
#
# Prerequisites:
#   - brain.exe must be on PATH (test: brain --version)
#   - BRAIN_AUDIT_KEY_PEM must be available in the environment or Windows
#     Credential Manager (entry: profile-a-brain-audit)
#
# Uninstall:
#   Unregister-ScheduledTask -TaskName "brain-daily-brief" -Confirm:$false
#
# Threat model: the signing key is stored in Windows Credential Manager and
# injected as an env var at task-run time. It is NEVER embedded in this script
# or in the task XML. See docs/operations/s09-evidence.md § Scheduled-task
# threat model.
param(
    [Parameter(Mandatory=$true)]
    [string]$VaultPath,

    [string]$BrainExe   = "brain",
    [string]$TaskName   = "brain-daily-brief",
    [string]$LogDir     = "$env:USERPROFILE\.brain\logs"
)
$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Resolve signing key from environment or Windows Credential Manager.
$auditKey = $env:BRAIN_AUDIT_KEY_PEM
if (-not $auditKey) {
    try {
        $cred = [System.Net.NetworkCredential]::new("", (
            (cmdkey /list | Select-String "profile-a-brain-audit") | ForEach-Object {
                (cmdkey /generic:profile-a-brain-audit /user:$env:USERNAME /show 2>$null).Password
            }
        )).Password
        $auditKey = $cred
    } catch { }
}
if (-not $auditKey) {
    Write-Warning "BRAIN_AUDIT_KEY_PEM not set and no Credential Manager entry found."
    Write-Warning "Captures will not be drained (drain skips unsigned)."
    Write-Warning "Store the key: cmdkey /add:profile-a-brain-audit /user:$env:USERNAME /pass:<PEM>"
    $auditKey = ""
}

$logFile = "$LogDir\brief-$(Get-Date -Format 'yyyy-MM-dd').log"

# Task action: the `maintain` umbrella -- sync --publish + brief, PLUS the
# date-gated branches (Mon=health, Tue=integrity, Sun=digest,
# 1st=graphify-documented-only). This is THE single sanctioned OS task
# (`brain-nightly`, persistence-budget.md THE LOCK) -- see
# src/brain/core.py BrainCore.maintain. routines/manifest.json id "brain-nightly".
$scriptBlock = @"
`$env:BRAIN_VAULT = '$VaultPath'
`$env:BRAIN_AUDIT_KEY_PEM = '$auditKey'
`$log = '$LogDir\brief-' + (Get-Date -Format 'yyyy-MM-dd') + '.log'
'=== brain-daily-brief / brain-nightly ' + (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ') + ' ===' | Out-File -Append -FilePath `$log
& '$BrainExe' maintain --json 2>&1 | Out-File -Append -FilePath `$log
# Rotate logs older than 30 days
Get-ChildItem '$LogDir\brief-*.log' | Where-Object { `$_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force
"@

$action   = New-ScheduledTaskAction -Execute "powershell.exe" `
              -Argument "-NonInteractive -WindowStyle Hidden -Command `"$scriptBlock`""
$trigger  = New-ScheduledTaskTrigger -Daily -At "07:00"
$settings = New-ScheduledTaskSettingsSet `
              -RunOnlyIfNetworkAvailable:$false `
              -StartWhenAvailable `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Limited -Force | Out-Null

Write-Host "Installed: $TaskName (daily 07:00)"
Write-Host "  Logs:      $LogDir\brief-YYYY-MM-DD.log"
Write-Host "  Status:    Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Dry-run:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Uninstall: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
