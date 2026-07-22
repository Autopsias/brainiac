#!/usr/bin/env bash
# brain-retrieval-watch.sh — monthly DETERMINISTIC retrieval-quality drift watch
# (owner ruling 2026-07-20: scheduled measurement is automatic; parameter TUNING
# stays on-invoke via /autoresearch, per that skill's own contract).
#
# What it does (no model, no network, read-only against the index):
#   1. capture ONE hybrid-search run over eval/golden_set.json at the CURRENT
#      shipped defaults (eval/capture_run.py)
#   2. score it against the stored baseline with eval/harness_direct.py and
#      eval/gate.py's non-inferiority gate
#   3. PASS  -> evidence file + one health-history-adjacent log line, silent
#      FAIL  -> hot.md escalation line + osascript notification: "retrieval
#               quality regressed — run /autoresearch"
#
# Baseline = eval/runs/retrieval-watch-baseline.json (created on first run).
# Evidence = eval/runs/retrieval-watch-<date>.json (+ .verdict.txt)
# Schedule: monthly via launchd (com.brainiac.retrievalwatch) — installed BY
# THE OWNER; this script never self-registers. See the render/install one-liner
# in the header of scripts/com.brainiac.retrievalwatch.plist.
#
# Usage: BRAIN_VAULT=/path/to/vault bash scripts/brain-retrieval-watch.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VAULT="${BRAIN_VAULT:?BRAIN_VAULT must be set (path to your brain vault)}"
PY="$REPO/.venv/bin/python"
RUNS="$REPO/eval/runs"
GOLDEN="$REPO/eval/golden_set.json"
QRELS="$REPO/eval/qrels"
BASE="$RUNS/retrieval-watch-baseline.json"
TODAY="$(date +%F)"
OUT="$RUNS/retrieval-watch-$TODAY.json"
HOT="$VAULT/.brain/memory/hot.md"
log() { echo "[retrieval-watch $(date +%H:%M:%S)] $*"; }

[ -f "$GOLDEN" ] || { log "no golden set at $GOLDEN — nothing to measure"; exit 0; }
mkdir -p "$RUNS"

log "capturing current-defaults run over golden set"
BRAIN_VAULT="$VAULT" "$PY" "$REPO/eval/capture_run.py" \
  --golden "$GOLDEN" --system brain-defaults --out "$OUT"

if [ ! -f "$BASE" ]; then
  cp "$OUT" "$BASE"
  log "first run — baseline established at $BASE"
  exit 0
fi

log "gating current vs baseline (non-inferiority)"
VERDICT="$OUT.verdict.txt"
if "$PY" "$REPO/eval/harness_direct.py" --golden "$GOLDEN" --qrels "$QRELS" \
     --current "$BASE" --new "$OUT" --out "$OUT.compare.json" \
   && "$PY" "$REPO/eval/gate.py" "$OUT.compare.json" > "$VERDICT" 2>&1; then
  log "PASS — retrieval quality non-inferior to baseline"
  exit 0
fi

log "FAIL — retrieval quality regressed vs baseline"
{
  echo ""
  echo "## $TODAY — retrieval-quality watch: REGRESSION"
  echo "- gate.py failed current-defaults vs baseline ($(basename "$OUT"))"
  echo "- evidence: eval/runs/$(basename "$OUT") + .compare.json + .verdict.txt"
  echo "- action: run /autoresearch (on-invoke tuning) in the engine repo; if a"
  echo "  change is KEPT, apply it as a reviewed code change and refresh the"
  echo "  baseline (cp the new run over retrieval-watch-baseline.json)"
} >> "$HOT"
osascript -e 'display notification "retrieval quality regressed vs baseline — run /autoresearch" with title "brainiac retrieval-watch"' 2>/dev/null || true
exit 1
