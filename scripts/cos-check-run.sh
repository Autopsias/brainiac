#!/usr/bin/env bash
# Read the LAST COS run's own artifacts and say what it actually did.
#
# WHY THIS EXISTS. A run report is the run's account of itself. This reads the
# ledgers and the vault instead, because the failure this whole plan started
# with was a phase that reported PASS while doing nothing — and the check that
# was supposed to catch it could not fail.
#
# Usage: scripts/cos-check-run.sh [vault]
set -euo pipefail

VAULT="${1:-${BRAIN_VAULT:?set BRAIN_VAULT or pass the vault path as \$1}}"
OPS="${VAULT}/cos-ops"
[[ -d "$OPS" ]] || { echo "no cos-ops at $OPS" >&2; exit 1; }

LEDGER="$(ls -t "$OPS"/_cos_ingestion_ledger_*.jsonl 2>/dev/null | head -1 || true)"
REPORT="$(ls -t "$OPS"/_cos_nightly_*.md 2>/dev/null | head -1 || true)"

echo "=== 1. DID THE EXTRACTION PHASE RUN AT ALL? ==="
if [[ -z "$LEDGER" ]]; then
  echo "  NO INGESTION LEDGER — on a mail-live night this is a FAIL, not a quiet"
  echo "  night. This is the exact silent-skip the v5.36 run-obligation gate exists"
  echo "  to catch. Check the run report's BLOCKED block for why."
  [[ -n "$REPORT" ]] && echo "  report: $REPORT"
  exit 1
fi
echo "  ledger: $(basename "$LEDGER")  ($(date -r "$LEDGER" '+%Y-%m-%d %H:%M'))"

echo
echo "=== 2. WHAT DID IT DECIDE, PER THREAD? ==="
python3 - "$LEDGER" <<'PY'
import collections, json, sys, pathlib
rows = [json.loads(l) for l in pathlib.Path(sys.argv[1]).read_text().splitlines() if l.strip()]
print(f"  {len(rows)} in-scope thread(s)")
for field, label in (("disposition", "disposition"), ("category", "category"),
                     ("held_reason", "held reason")):
    counts = collections.Counter(r.get(field) or "-" for r in rows)
    if len(counts) > 1 or "-" not in counts:
        print(f"  by {label}:")
        for k, n in counts.most_common():
            print(f"      {n:>4}  {k}")
cands = [r for r in rows if r.get("disposition") == "candidate"]
print(f"  => {len(cands)} candidate(s) staged")
# The honest reading: zero candidates is only OK if every thread was
# genuinely ineligible, and the ledger has to SAY so per thread.
if not cands:
    unexplained = [r for r in rows if not (r.get("held_reason") or r.get("disposition"))]
    if unexplained:
        print(f"  WARNING: {len(unexplained)} row(s) give no reason for staging nothing")
PY

echo
echo "=== 3. DID ANYTHING REACH YOU? ==="
brain --vault "$VAULT" inbox 2>/dev/null | head -6 || echo "  (brain inbox unavailable)"

echo
echo "=== 4. ATTACHMENT LANE ==="
# Read the dir the NIGHTLY JOB actually uses, not just this shell's env —
# an interactive shell rarely has it exported, and printing "<unset>" implies
# the lane is unconfigured when it is configured where it matters.
DL="${BRAIN_COS_DOWNLOADS_DIR:-$(plutil -extract EnvironmentVariables.BRAIN_COS_DOWNLOADS_DIR raw -o - \
      ~/Library/LaunchAgents/com.brainiac.nightly.*.plist 2>/dev/null | head -1)}"
if [[ -n "$DL" && -d "$DL" ]]; then
  echo "  staging dir : $DL ($(ls -1 "$DL" 2>/dev/null | wc -l | tr -d ' ') file(s) waiting)"
else
  echo "  staging dir : NOT CONFIGURED — the attachment lane cannot carry anything"
fi
MAN="$(ls -t "$VAULT"/.brain/cos/drop/ingest-manifest/manifest-*.jsonl 2>/dev/null | head -1 || true)"
if [[ -n "$MAN" ]]; then
  echo "  last manifest: $(basename "$MAN") ($(date -r "$MAN" '+%Y-%m-%d'))"
else
  echo "  last manifest: none ever written"
fi
grep -oE '"attachment_lane"[^,]*' "$REPORT" 2>/dev/null | head -1 | sed 's/^/  reported: /' || true

echo
echo "=== 5. WHAT THE RUN SAID ABOUT ITSELF (compare with the above) ==="
# "no report" and "a report that says none of these things" are DIFFERENT
# findings and must not print the same line: the first is a missing artifact,
# the second is a report that went quiet about its own counters. Conflating
# them is the same can't-tell-them-apart shape this whole script exists to
# break — measured on run 61, whose report existed and was read.
if [[ -z "$REPORT" ]]; then
  echo "  no run report found"
else
  echo "  report: $(basename "$REPORT")"
  SAID="$(grep -iE "ingestion_candidates|ingestion|attachment_lane|OUTCOME CONTRACT|outcome checker|verdict" \
          "$REPORT" | head -6 || true)"
  if [[ -n "$SAID" ]]; then
    printf '%s\n' "$SAID" | sed 's/^/  /'
  else
    echo "  the report names NONE of its own counters (ingestion/attachment/outcome)"
  fi
fi
