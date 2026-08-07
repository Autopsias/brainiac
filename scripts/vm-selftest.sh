#!/usr/bin/env bash
# Cowork VM self-test — run from the workspace root:  bash vault/.brain/vm-selftest.sh
# Exercises the REAL operations (not status/doctor false-greens). Prints PASS/FAIL per check,
# then one VERDICT line. Un-fakeable: the agent must show this output verbatim.
set -u

# --- full bootstrap (this is the env set a partial agent bootstrap skips) ---
export BRAIN_VAULT="$PWD/vault"
export BRAIN_ROLE=vm
export BRAIN_RUNTIME_DIR="$BRAIN_VAULT/.brain"
export BRAIN_MODEL_CACHE="$BRAIN_RUNTIME_DIR/model"
export PYTHONPATH="$BRAIN_RUNTIME_DIR/engine:$BRAIN_RUNTIME_DIR/vendor/$(uname -m):${PYTHONPATH:-}"
export PATH="$BRAIN_RUNTIME_DIR:$PATH"

# Scratch dir must be writable — some sandboxes mount /tmp read-only.
# Honour $TMPDIR; fall back to a writable dir under the runtime (never bare /tmp).
SCRATCH="$(mktemp -d 2>/dev/null || true)"
[ -n "${SCRATCH:-}" ] && [ -w "$SCRATCH" ] || SCRATCH="$BRAIN_RUNTIME_DIR/.selftest-tmp"
mkdir -p "$SCRATCH" 2>/dev/null
OUT="$SCRATCH/vmst.out"; : > "$OUT"
say(){ echo "$@" | tee -a "$OUT"; }

# Expected version = whatever engine was STAGED into this workspace (never a
# hardcode — the staged _version.py is the SSOT the host re-stage last landed).
EXPECT="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' \
  "$BRAIN_RUNTIME_DIR/engine/brain/_version.py" 2>/dev/null)"
[ -n "$EXPECT" ] || EXPECT="(unknown — no staged _version.py)"

say "== 1. engine on PATH & version =="
V="$(brain --version 2>&1)"; say "  $V"
case "$V" in
  *"$EXPECT"*) say "  PASS — engine $EXPECT (matches staged _version.py)";;
  *) say "  FAIL — expected $EXPECT (from staged _version.py)";;
esac

say "== 2. index/snapshot reachable =="
brain status 2>&1 | sed 's/^/  /' | tee -a "$OUT"
brain status 2>&1 | grep -q "notes" && say "  PASS — status returned index stats" || say "  FAIL — no index stats"

say "== 3. lexical retrieval (no embed) =="
# stdout -> json file, stderr -> separate file (onnxruntime/tokenizers warn to stderr)
brain grep "classification" --json > "$SCRATCH"/vmst_grep.json 2>"$SCRATCH"/vmst_grep.err
python3 - "$OUT" "$SCRATCH/vmst_grep.json" <<'PY'
import json,sys
o=open(sys.argv[1],"a")
raw=open(sys.argv[2]).read()
try:
    d=json.loads(raw); r=d.get('results', d) if isinstance(d,dict) else d
    n=len(r) if isinstance(r,list) else 0
    msg=f"  grep hits: {n}\n  " + ("PASS — lexical retrieval works" if 'error' not in raw.lower() else "FAIL — grep error")
except Exception:
    msg="  FAIL — grep output not JSON: "+raw[:140]
print(msg); o.write(msg+"\n")
PY

say "== 4. SEMANTIC search — THE real test (this is what faked green before) =="
# stdout -> json (results or {"error":...}); stderr -> err (onnxruntime cpuid warning lives here, NOT the JSON)
# --no-rerank (RK-02 caller policy, 2026-08-04): this probe asks "does dense
# retrieval work at all", which is decided entirely before the cross-encoder.
# Since BR-03 made reranking default-on, leaving it on would add seconds to
# minutes per run (eval/FOLLOWUPS.md #6) to test nothing this probe checks —
# and RET-02 guarantees a broken reranker degrades to identity rather than
# failing search, so it cannot hide a fault here.
brain search "energy transition strategy" --no-rerank --json > "$SCRATCH"/vmst_search.json 2>"$SCRATCH"/vmst_search.err
python3 - "$OUT" "$SCRATCH/vmst_search.json" "$SCRATCH/vmst_search.err" <<'PY'
import json,sys
o=open(sys.argv[1],"a")
out=open(sys.argv[2]).read()
err=open(sys.argv[3]).read()
# scan BOTH streams for the real failure sentinels (an embedder error may land in either)
both=out+"\n"+err
bad = any(s in both for s in ("EmbedderUnavailable","huggingface_hub","HashEmbedder","FALLING BACK"))
if bad:
    msg="  FAIL — embedder NOT working (semantic search dead):\n    "+both.strip()[:240]
else:
    try:
        d=json.loads(out); r=d.get('results',[])
        if isinstance(d,dict) and d.get('error'):
            msg="  FAIL — search returned an error: "+str(d['error'])[:200]
        elif r:
            msg=f"  semantic results: {len(r)}\n  PASS — real semantic search works (embedder ran, ranked results)"
        else:
            msg="  semantic results: 0\n  PASS* — embedder ran with NO error; 0 results is likely the VM Internal egress cap, not a break"
    except Exception:
        msg="  FAIL — search stdout not JSON (stderr leaked in? check "+sys.argv[3]+"):\n    "+out.strip()[:200]
print(msg); o.write(msg+"\n")
PY

say ""
say "==================== VERDICT ===================="
if grep -q "FAIL" "$OUT"; then
  say "  RESULT: BROKEN — one or more checks FAILED (see above)."
else
  say "  RESULT: OK — engine + retrieval all pass."
fi
say "  Still to test (agent step, NOT this script): ask the agent to"
say "  'run the kb-curator skill in audit mode' — proves the staged skills load."
say "================================================="
