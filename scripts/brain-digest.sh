#!/usr/bin/env bash
# brain-weekly-digest — weekly brain digest (UX-02)
#
# Task name:    brain-weekly-digest
# User context: current user (LaunchAgent / Task Scheduler user-level)
# Schedule:     weekly Sunday 07:00 (or call from brain-brief.sh on Sundays)
# Logs:         $BRAIN_LOG_DIR/digest-YYYY-MM-DD.log  (90-day rotation)
# Uninstall:    see installer scripts
set -euo pipefail

LOGDIR="${BRAIN_LOG_DIR:-$HOME/.brain/logs}"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/digest-$(date +%Y-%m-%d).log"

{
  echo "=== brain-weekly-digest $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  brain digest --json
} >> "$LOG" 2>&1

# Rotate logs older than 90 days.
find "$LOGDIR" -name 'digest-*.log' -mtime +90 -delete 2>/dev/null || true
