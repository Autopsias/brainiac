#!/usr/bin/env bash
# brain-retrieval-watch.sh — monthly DETERMINISTIC retrieval-quality drift watch
# (owner ruling 2026-07-20: scheduled measurement is automatic; parameter TUNING
# stays on-invoke via /autoresearch, per that skill's own contract).
#
# What it does (no model, no network, read-only against the index):
#   1. capture ONE hybrid-search run over eval/golden_set.json at the CURRENT
#      shipped defaults (eval/capture_run.py --shipped-defaults: rerank=True,
#      rerank_top=20; fails loud if reranker is unavailable)
#   2. verify capture-mode parity against the pinned baseline (refuses silent
#      cross-mode bare-vs-reranked comparison; instructs --rebaseline)
#   3. score it against the pinned baseline with eval/harness_direct.py and
#      eval/gate.py's non-inferiority gate
#   4. PASS  -> evidence file + log line, silent; NEVER overwrites $BASE
#      FAIL  -> hot.md escalation line + osascript notification: "retrieval
#               quality regressed — run /autoresearch"
#
# Baseline = eval/runs/retrieval-watch-baseline.json (created on first run,
#            updated ONLY when invoked with --rebaseline).
# Evidence = eval/runs/retrieval-watch-<date>.json (+ .verdict.txt)
# Schedule: monthly via launchd (com.brainiac.retrievalwatch) — installed BY
# THE OWNER; this script never self-registers. See the render/install one-liner
# in the header of scripts/com.brainiac.retrievalwatch.plist.
#
# Usage:
#   BRAIN_VAULT=/path/to/vault bash scripts/brain-retrieval-watch.sh
#   BRAIN_VAULT=/path/to/vault bash scripts/brain-retrieval-watch.sh --rebaseline
set -euo pipefail

REBASELINE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --rebaseline) REBASELINE=1 ;;
    *)
      echo "[retrieval-watch] ERROR — unknown argument: $1 (supported: --rebaseline)" >&2
      exit 2
      ;;
  esac
  shift
done

REPO="${BRAIN_WATCH_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
VAULT="${BRAIN_VAULT:?BRAIN_VAULT must be set (path to your brain vault)}"
PY="${BRAIN_WATCH_PYTHON:-$REPO/.venv/bin/python}"
RUNS="${BRAIN_WATCH_RUNS:-$REPO/eval/runs}"
GOLDEN="${BRAIN_WATCH_GOLDEN:-$REPO/eval/golden_set.json}"
QRELS="${BRAIN_WATCH_QRELS:-$REPO/eval/qrels/qrels.json}"
BASE="${BRAIN_WATCH_BASE:-$RUNS/retrieval-watch-baseline.json}"
TODAY="${BRAIN_WATCH_TODAY:-$(date +%F)}"
OUT="${BRAIN_WATCH_OUT:-$RUNS/retrieval-watch-$TODAY.json}"
CAPTURE_SCRIPT="${BRAIN_WATCH_CAPTURE_SCRIPT:-$REPO/eval/capture_run.py}"
# `brain alerts` reads this; hot.md alone is a log nobody has to open.
STATE="${BRAIN_WATCH_STATE:-$HOME/.brain/retrieval-watch-state.json}"
state() { mkdir -p "$(dirname "$STATE")"; printf '{"at": "%s", "verdict": "%s", "run": "%s"}\n' "$TODAY" "$1" "$(basename "$OUT")" > "$STATE"; }
HOT="${BRAIN_WATCH_HOT:-$VAULT/.brain/memory/hot.md}"
COMPARE="$OUT.compare.json"
VERDICT="$OUT.verdict.txt"
log() { echo "[retrieval-watch $(date +%H:%M:%S)] $*"; }

[ -f "$GOLDEN" ] || { log "no golden set at $GOLDEN — nothing to measure"; exit 0; }
mkdir -p "$RUNS" "$(dirname "$OUT")" "$(dirname "$BASE")"

COLLISION_RC=0
"$PY" - "$BASE" "$STATE" "$OUT" "$COMPARE" "$VERDICT" "$HOT" <<'PYEOF' || COLLISION_RC=$?
import os, sys

base_raw, state_raw, out_raw, compare_raw, verdict_raw, hot_raw = sys.argv[1:7]
base_real = os.path.realpath(base_raw)

def resolves_to_base(target_raw: str) -> bool:
    target_real = os.path.realpath(target_raw)
    return (
        (os.path.exists(target_raw) and os.path.exists(base_raw) and os.path.samefile(target_raw, base_raw))
        or base_real == target_real
        or (
            os.path.exists(os.path.dirname(target_real))
            and os.path.exists(os.path.dirname(base_real))
            and os.path.samefile(os.path.dirname(target_real), os.path.dirname(base_real))
            and os.path.basename(target_real).casefold() == os.path.basename(base_real).casefold()
        )
    )

if resolves_to_base(state_raw):
    print(
        f"ERROR — BRAIN_WATCH_STATE ({state_raw}) resolves to the pinned baseline "
        f"({base_raw}); refusing to overwrite the baseline.",
        file=sys.stderr,
    )
    sys.exit(3)

for label, target_raw in (
    ("BRAIN_WATCH_OUT", out_raw),
    ("compare output", compare_raw),
    ("verdict output", verdict_raw),
    ("BRAIN_WATCH_HOT", hot_raw),
):
    if resolves_to_base(target_raw):
        print(
            f"ERROR — {label} ({target_raw}) resolves to the pinned baseline "
            f"({base_raw}); refusing to overwrite the baseline directly.",
            file=sys.stderr,
        )
        sys.exit(2)
PYEOF
if [ "$COLLISION_RC" -eq 3 ]; then
  log "ERROR — state output path resolves to pinned baseline $BASE; refusing to run" >&2
  exit 2
elif [ "$COLLISION_RC" -ne 0 ]; then
  log "ERROR — output path resolves to pinned baseline $BASE; refusing to run" >&2
  state ERROR
  exit 2
fi

TMP_OUT="$(mktemp "$(dirname "$OUT")/.retrieval-watch.tmp.XXXXXX")"
TMP_COMPARE="$(mktemp "$(dirname "$OUT")/.retrieval-watch.compare.XXXXXX")"
TMP_VERDICT="$(mktemp "$(dirname "$OUT")/.retrieval-watch.verdict.XXXXXX")"
TMP_BASE=""
trap 'rm -f "${TMP_OUT:-}" "${TMP_COMPARE:-}" "${TMP_VERDICT:-}" "${TMP_BASE:-}"' EXIT

log "capturing shipped-defaults run over golden set (rerank=True, rerank_top=20)"
CAPTURE_RC=0
BRAIN_VAULT="$VAULT" "$PY" "$CAPTURE_SCRIPT" \
  --golden "$GOLDEN" --system brain-defaults --shipped-defaults --out "$TMP_OUT" || CAPTURE_RC=$?
if [ "$CAPTURE_RC" -ne 0 ]; then
  rm -f "$TMP_OUT"
  log "ERROR — capture failed (rc=$CAPTURE_RC); refusing to leave stale watch status"
  state ERROR
  exit "$CAPTURE_RC"
fi

write_baseline_atomic() {
  TMP_BASE="$(mktemp "${BASE}.tmp.XXXXXX")"
  cp "$TMP_OUT" "$TMP_BASE"
  mv -f "$TMP_BASE" "$BASE"
  mv -f "$TMP_OUT" "$OUT"
  log "$1"
  state BASELINE
  exit 0
}

if [ "$REBASELINE" -eq 1 ]; then
  write_baseline_atomic "rebaselined — pinned baseline updated at $BASE"
fi

if [ ! -f "$BASE" ]; then
  write_baseline_atomic "first run — pinned baseline established at $BASE"
fi

if ! "$PY" - "$BASE" "$TMP_OUT" <<'PYEOF'
import json, sys

def capture_rerank_settings(path: str) -> dict:
    data = json.loads(open(path, encoding="utf-8").read())
    params = (data.get("index_state") or {}).get("params") or {}
    if "rerank" in data and isinstance(data["rerank"], bool):
        rerank = data["rerank"]
    elif "rerank" in params and isinstance(params["rerank"], bool):
        rerank = params["rerank"]
    else:
        rerank = False
    rerank_top = data.get("rerank_top")
    if rerank_top is None:
        rerank_top = params.get("rerank_top")
    rerank_backend = data.get("rerank_backend") or params.get("rerank_backend")
    rerank_model = data.get("rerank_model") or params.get("rerank_model")
    rerank_onnx_dir = data.get("rerank_onnx_dir")
    if rerank_onnx_dir is None:
        rerank_onnx_dir = params.get("rerank_onnx_dir")
    if "rerank_gate_disabled" in data and isinstance(data["rerank_gate_disabled"], bool):
        rerank_gate_disabled = data["rerank_gate_disabled"]
    elif "rerank_gate_disabled" in params and isinstance(params["rerank_gate_disabled"], bool):
        rerank_gate_disabled = params["rerank_gate_disabled"]
    elif "rerank_gate" in params and isinstance(params["rerank_gate"], bool):
        rerank_gate_disabled = not params["rerank_gate"]
    else:
        rerank_gate_disabled = False
    return {
        "rerank": rerank,
        "rerank_top": rerank_top,
        "rerank_backend": rerank_backend,
        "rerank_model": rerank_model,
        "rerank_onnx_dir": rerank_onnx_dir,
        "rerank_gate_disabled": rerank_gate_disabled,
    }

base_cfg = capture_rerank_settings(sys.argv[1])
out_cfg = capture_rerank_settings(sys.argv[2])
diffs = []
if base_cfg["rerank"] != out_cfg["rerank"]:
    diffs.append(
        f"baseline rerank={str(base_cfg['rerank']).lower()} vs "
        f"current rerank={str(out_cfg['rerank']).lower()}"
    )
if base_cfg["rerank_top"] != out_cfg["rerank_top"]:
    diffs.append(
        f"baseline rerank_top={base_cfg['rerank_top']} vs "
        f"current rerank_top={out_cfg['rerank_top']}"
    )
if base_cfg["rerank_gate_disabled"] != out_cfg["rerank_gate_disabled"]:
    diffs.append(
        f"baseline rerank_gate_disabled={str(base_cfg['rerank_gate_disabled']).lower()} vs "
        f"current rerank_gate_disabled={str(out_cfg['rerank_gate_disabled']).lower()}"
    )
if base_cfg["rerank_onnx_dir"] != out_cfg["rerank_onnx_dir"]:
    diffs.append(
        f"baseline rerank_onnx_dir={base_cfg['rerank_onnx_dir']} vs "
        f"current rerank_onnx_dir={out_cfg['rerank_onnx_dir']}"
    )
for key in ("rerank_backend", "rerank_model"):
    if (
        base_cfg[key] is not None
        and out_cfg[key] is not None
        and base_cfg[key] != out_cfg[key]
    ):
        diffs.append(f"baseline {key}={base_cfg[key]} vs current {key}={out_cfg[key]}")

if diffs:
    print(
        f"ERROR — capture mode mismatch ({'; '.join(diffs)}); "
        "refusing silent cross-mode comparison. "
        "Run with --rebaseline to establish a matching baseline.",
        file=sys.stderr,
    )
    sys.exit(2)
PYEOF
then
  log "ERROR — capture mode mismatch against $BASE; run with --rebaseline to replace the baseline"
  state ERROR
  exit 2
fi

log "gating current vs pinned baseline (non-inferiority)"
if ! "$PY" "$REPO/eval/harness_direct.py" --golden "$GOLDEN" --qrels "$QRELS" \
     --current "$BASE" --new "$TMP_OUT" --out "$TMP_COMPARE"; then
  # A comparison that never ran measured nothing: an ERROR, never a regression.
  log "ERROR — comparison did not run (see stderr log); nothing was measured"
  state ERROR
  exit 2
fi
GATE_RC=0
"$PY" "$REPO/eval/gate.py" --scorecard "$TMP_COMPARE" > "$TMP_VERDICT" 2>&1 || GATE_RC=$?
mv -f "$TMP_OUT" "$OUT"
mv -f "$TMP_COMPARE" "$COMPARE"
mv -f "$TMP_VERDICT" "$VERDICT"
if [ "$GATE_RC" -eq 0 ]; then
  log "PASS — retrieval quality non-inferior to pinned baseline (baseline left unchanged)"
  state PASS
  exit 0
fi
if [ "$GATE_RC" -ne 1 ]; then
  # gate.py exit 2 = undecidable (e.g. >10% corpus note-count drift or older harness).
  log "ERROR — gate undecidable (rc=$GATE_RC, see $(basename "$VERDICT")); if the vault corpus drifted >10%, run with --rebaseline to establish a fresh baseline"
  state ERROR
  exit 2
fi

log "FAIL — retrieval quality regressed vs pinned baseline"
state FAIL
{
  echo ""
  echo "## $TODAY — retrieval-quality watch: REGRESSION"
  echo "- gate.py failed current-defaults vs baseline ($(basename "$OUT"))"
  echo "- evidence: eval/runs/$(basename "$OUT") + .compare.json + .verdict.txt"
  echo "- action: run /autoresearch (on-invoke tuning) in the engine repo; if a"
  echo "  change is KEPT, apply it as a reviewed code change and refresh the"
  echo "  baseline (run scripts/brain-retrieval-watch.sh --rebaseline)"
} >> "$HOT"
osascript -e 'display notification "retrieval quality regressed vs baseline — run /autoresearch" with title "brainiac retrieval-watch"' 2>/dev/null || true
exit 1
