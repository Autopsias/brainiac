#!/usr/bin/env bash
# ponytail: one-shot health check for the detached s13-final full-corpus
# capture (see scripts/eval-s13-launch.sh). Reports: process alive?, elapsed,
# log tail, artifact present? Run it any time; it does not block.
#
# Usage: scripts/eval-s13-monitor.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$REPO/.brain/eval-tmp"
LOCK="$REPO/_evidence/eval/capture.lock"
HEARTBEAT="$REPO/_evidence/eval/heartbeat.txt"
DONE_SENTINEL="$REPO/_evidence/eval/DONE"
ARTIFACT="$TMP_DIR/s13-final.json.partial"
LOG="$TMP_DIR/s13-final.log"

echo "=== s13-final capture monitor  $(date -u +%FT%TZ) ==="

if [[ -f "$LOCK" ]]; then
    PID="$(cat "$LOCK" 2>/dev/null || echo "")"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        ELAPSED=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK") ))
        echo "process: ALIVE (pid $PID), elapsed ${ELAPSED}s"
    else
        echo "process: NOT RUNNING (stale lock, pid $PID)"
    fi
else
    echo "process: NO LOCKFILE (never launched, or already cleaned up)"
fi

echo
if [[ -f "$HEARTBEAT" ]]; then
    echo "--- heartbeat ---"
    cat "$HEARTBEAT"
else
    echo "heartbeat: MISSING ($HEARTBEAT)"
fi

echo
if [[ -f "$DONE_SENTINEL" ]]; then
    echo "--- DONE sentinel present ---"
    cat "$DONE_SENTINEL"
else
    echo "DONE sentinel: not yet written (job still in-flight, or not launched)"
fi

echo
if [[ -f "$ARTIFACT" ]]; then
    echo "artifact: PRESENT ($(du -h "$ARTIFACT" | cut -f1)) at $ARTIFACT"
else
    echo "artifact: absent at $ARTIFACT"
fi

echo
if [[ -f "$LOG" ]]; then
    echo "--- log tail (last 20 lines) ---"
    tail -20 "$LOG"
else
    echo "log: missing ($LOG)"
fi
