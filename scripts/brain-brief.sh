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
# Task name:    brain-daily-brief (per-vault launchd label com.brainiac.nightly.<id> /
#               Windows task brain-daily-brief-<id>) -- the manifest entry for this
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
# Security note: the audit signing key is resolved AT RUNTIME by
# resolve_signing_key() (env BRAIN_AUDIT_KEY_PEM -> keychain fallthrough).
# The key is NEVER stored in this file, and on macOS it is no longer baked
# into the launchd plist either — the drain reads the Keychain when it runs
# (fails closed if locked/absent). BRAIN_AUDIT_KEY_PEM in the plist env is
# only populated when the operator explicitly injected it at install time.
# Windows: same — the task action carries no key; brain resolves it from the
# Credential Manager (keyring) at runtime.
#
# Threat model: see docs/operations/s09-evidence.md § Scheduled-task threat model.
set -euo pipefail

LOGDIR="${BRAIN_LOG_DIR:-$HOME/.brain/logs}"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/brief-$(date +%Y-%m-%d).log"

# Resolve the brain binary explicitly: launchd runs with a minimal PATH
# (/usr/bin:/bin:...) that does NOT include ~/.local/bin or the install venv,
# so a bare `brain` fails with "command not found" under the scheduled run.
# Prefer the canonical install venv; fall back to PATH for dev/manual runs.
BRAIN_BIN="${BRAIN_BIN:-}"
if [ -z "$BRAIN_BIN" ]; then
  if [ -x "$HOME/.brainiac/venv/bin/brain" ]; then
    BRAIN_BIN="$HOME/.brainiac/venv/bin/brain"
  else
    BRAIN_BIN="$(command -v brain || true)"
  fi
fi
if [ -z "$BRAIN_BIN" ]; then
  echo "brain-brief: no brain binary found (venv missing and not on PATH)" >> "$LOG"
  exit 1
fi

{
  echo "=== brain-daily-brief / brain-nightly $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  # maintain: sync --publish (drain pending captures + reconcile index +
  # republish snapshot) + brief, THEN whichever of health/integrity/digest is
  # due today (date-gated). graphify is documented-only (no build invoked --
  # the discovery graph stays separate tooling per task-disposition.md row 7).
  "$BRAIN_BIN" maintain --json
} >> "$LOG" 2>&1

# Rotate logs older than 30 days.
find "$LOGDIR" -name 'brief-*.log' -mtime +30 -delete 2>/dev/null || true
