#!/usr/bin/env bash
# Detached full-corpus s13-final capture launcher (EVAL-01, task 2).
# ponytail: single script instead of separate launcher+heartbeat-daemon —
# the launched process itself updates the heartbeat every 30s in a background
# loop of its own process group, so one setsid covers everything.
#
# Guards against a still-alive prior capture holding the output fd (grill-3):
# refuses to launch if _evidence/eval/capture.lock names a live PID.
#
# Writes:
#   .brain/eval-tmp/s13-final.json.partial   (gitignored; s05 promotes it)
#   .brain/eval-tmp/s13-final.log            (stdout+stderr)
#   _evidence/eval/capture.lock              (PID, committable)
#   _evidence/eval/heartbeat.txt             (PID + progress ts + query count, committable)
#   _evidence/eval/DONE                      (exit code + final query count, written on exit)
#
# Resume/check: scripts/eval-s13-monitor.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

TMP_DIR="$REPO/.brain/eval-tmp"
EVID_DIR="$REPO/_evidence/eval"
LOCK="$EVID_DIR/capture.lock"
HEARTBEAT="$EVID_DIR/heartbeat.txt"
DONE_SENTINEL="$EVID_DIR/DONE"
ARTIFACT="$TMP_DIR/s13-final.json.partial"
LOG="$TMP_DIR/s13-final.log"
GOLDEN="$REPO/eval/golden_set.json"
VAULT="${BRAIN_REFERENCE_VAULT:?set to your reference vault path}"
PY="$REPO/.venv-embed/bin/python3"

mkdir -p "$TMP_DIR" "$EVID_DIR"

# --- grill-3 guard: refuse if a prior capture still holds the output fd ---
if [[ -f "$LOCK" ]]; then
    OLD_PID="$(cat "$LOCK" 2>/dev/null || echo "")"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "REFUSING to launch: lockfile $LOCK names live PID $OLD_PID" >&2
        exit 1
    fi
    echo "stale lock found (pid $OLD_PID not running) — removing" >&2
    rm -f "$LOCK"
fi
if pgrep -f "capture_run.py.*s13-final" >/dev/null 2>&1; then
    echo "REFUSING to launch: a capture_run.py s13-final process is already running (pgrep hit)" >&2
    pgrep -fl "capture_run.py.*s13-final" >&2
    exit 1
fi

QUERY_COUNT="$("$PY" -c "import json;print(len(json.load(open('$GOLDEN'))['queries']))")"

# Inner runner: capture + heartbeat loop + DONE sentinel, all in one detached
# process group so it outlives this script and this session.
cat > "$TMP_DIR/_s13_inner.sh" <<INNER
#!/usr/bin/env bash
set -uo pipefail
PY="$PY"
GOLDEN="$GOLDEN"
VAULT="$VAULT"
ARTIFACT="$ARTIFACT"
LOG="$LOG"
HEARTBEAT="$HEARTBEAT"
DONE_SENTINEL="$DONE_SENTINEL"
QUERY_COUNT="$QUERY_COUNT"
MYPID=\$\$

# background heartbeat loop, dies with this shell (same process group)
( while true; do
    printf 'pid=%s\nprogress_ts=%s\nexpected_queries=%s\nartifact_exists=%s\n' \
      "\$MYPID" "\$(date -u +%FT%TZ)" "\$QUERY_COUNT" \
      "\$([[ -f "\$ARTIFACT" ]] && echo true || echo false)" > "\$HEARTBEAT.tmp"
    mv "\$HEARTBEAT.tmp" "\$HEARTBEAT"
    sleep 30
  done ) &
HB_PID=\$!

"\$PY" eval/capture_run.py \
  --golden "\$GOLDEN" \
  --vault "\$VAULT" \
  --system s13-final \
  --out "\$ARTIFACT" \
  --rebuild >> "\$LOG" 2>&1
EXIT_CODE=\$?

kill "\$HB_PID" 2>/dev/null || true

FINAL_N="\$("\$PY" -c "import json;print(len(json.load(open('\$ARTIFACT'))['runs']))" 2>/dev/null || echo 0)"
printf 'exit_code=%s\nfinished_ts=%s\nfinal_query_count=%s\nexpected_queries=%s\n' \
  "\$EXIT_CODE" "\$(date -u +%FT%TZ)" "\$FINAL_N" "\$QUERY_COUNT" > "\$DONE_SENTINEL"
rm -f "$LOCK"
INNER
chmod +x "$TMP_DIR/_s13_inner.sh"

# Launch fully detached: new session (setsid) so it survives this subagent
# exiting, caffeinate -i (+-s on AC power, per plan) to block idle/system
# sleep, nice so it doesn't starve the interactive machine.
CAFF_FLAGS="-i"
if pmset -g batt 2>/dev/null | head -1 | grep -qi "AC Power"; then
    CAFF_FLAGS="-i -s"
fi
# setsid (own session) on Linux; macOS has no setsid, so nohup+disown detaches
# from the controlling terminal/job table equivalently. Both survive this shell.
if command -v setsid >/dev/null 2>&1; then
    setsid caffeinate $CAFF_FLAGS nice -n 10 "$TMP_DIR/_s13_inner.sh" < /dev/null > /dev/null 2>&1 &
else
    nohup caffeinate $CAFF_FLAGS nice -n 10 bash "$TMP_DIR/_s13_inner.sh" < /dev/null > /dev/null 2>&1 &
fi
LAUNCH_PID=$!
disown || true

# give the caffeinate->nice->inner->capture_run.py chain time to spawn.
for _ in $(seq 1 10); do pgrep -f "capture_run.py.*s13-final" >/dev/null 2>&1 && break; sleep 1; done
REAL_PID="$(pgrep -f "capture_run.py.*s13-final" | head -1 || true)"
echo "${REAL_PID:-$LAUNCH_PID}" > "$LOCK"

echo "launched. launcher_pid=$LAUNCH_PID capture_pid=${REAL_PID:-unknown}"
echo "lockfile: $LOCK"
echo "log: $LOG"
echo "artifact (partial, gitignored): $ARTIFACT"
echo "heartbeat: $HEARTBEAT"
echo "resume/health check: scripts/eval-s13-monitor.sh"
