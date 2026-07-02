#!/usr/bin/env bash
# brain-daily-brief — THE single sanctioned host scheduled task (`brain-nightly`,
# persistence-budget.md THE LOCK). Runs the `brain maintain` umbrella, which is
# sync --publish + brief PLUS the date-gated branches (Mon=health, Tue=integrity,
# Sun=digest, 1st=graphify-documented-only) -- see src/brain/core.py
# BrainCore.maintain + src/brain/maintenance.py maintain_branches.
#
# Invoked by macOS launchd (brain-brief-mac.plist) or Windows Task Scheduler
# (install-brief-windows.ps1). This script is the ONE sanctioned automated
# brain invocation and the guaranteed daily capture-drain FLOOR.
#
# Task name:    brain-daily-brief (launchd label com.profile-a-brain.daily-brief /
#               Windows task brain-daily-brief) -- the manifest entry for this
#               task is routines/manifest.json id "brain-nightly". Stable IDs
#               are kept unchanged across this s07 script update so an existing
#               registration is NOT duplicated by the idempotent registrar
#               (scripts/register_tasks.py) -- only its invoked command changed.
# User context: current user (LaunchAgent / Task Scheduler user-level)
# Schedule:     daily 07:00 (see installer scripts); date-gated branches fire
#               INSIDE this one run -- see routines/manifest.json + persistence-budget.md.
# Logs:         $BRAIN_LOG_DIR/brief-YYYY-MM-DD.log  (30-day rotation)
# Uninstall:    see scripts/install-brief-mac.sh or install-brief-windows.ps1
#
# Security note: BRAIN_AUDIT_KEY_PEM must be injected from the OS keychain
# or env before this script runs. The key is NEVER stored in this file.
# macOS: the launchd plist carries the env var, populated at install time
#        from the macOS Keychain via security(1) or from BRAIN_AUDIT_KEY_PEM.
# Windows: the task action sets $env:BRAIN_AUDIT_KEY_PEM before invoking brain.
#
# Threat model: see docs/operations/s09-evidence.md § Scheduled-task threat model.
set -euo pipefail

LOGDIR="${BRAIN_LOG_DIR:-$HOME/.brain/logs}"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/brief-$(date +%Y-%m-%d).log"

{
  echo "=== brain-daily-brief / brain-nightly $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  # maintain: sync --publish (drain pending captures + reconcile index +
  # republish snapshot) + brief, THEN whichever of health/integrity/digest is
  # due today (date-gated). graphify is documented-only (no build invoked --
  # the discovery graph stays separate tooling per task-disposition.md row 7).
  brain maintain --json
} >> "$LOG" 2>&1

# Rotate logs older than 30 days.
find "$LOGDIR" -name 'brief-*.log' -mtime +30 -delete 2>/dev/null || true
