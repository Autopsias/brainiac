#!/usr/bin/env bash
# vm-boundary-probe.sh — NEGATIVE privilege probes, run from INSIDE the Cowork VM.
#
#   bash vault/.brain/vm-boundary-probe.sh          # from the workspace root
#
# scripts/vm-selftest.sh proves the VM leg WORKS (search, snapshot, embed).
# This proves the other half: that it correctly REFUSES everything the host
# broker holds. Both halves are needed — a VM that can search AND sign is a
# working VM with no boundary, and no capability test would notice.
#
# It must be run in the ACTUAL Cowork VM. A host shell with BRAIN_ROLE=vm
# exported is NOT a substitute: it shares the host filesystem, the host
# keychain and the host's process privileges, so it can pass every check here
# while proving nothing about the physical boundary. (Running it on the host
# DOES validate this script and the engine's role gate — that is a different,
# weaker claim, and it must be reported as such.)
#
# Every probe prints the refusal VERBATIM. Exit 0 = boundary intact.
#
# WHAT COUNTS AS A PASS (2026-07-31 rewrite). A non-zero exit is NOT a
# refusal. The previous version of this script invoked `brain write --id …`
# and `brain cos-correct --corrected-bucket …` — flags neither command has —
# so argparse exited 2 with a usage message and the probe scored PASS without
# the role gate ever being reached. Two vacuous passes out of fourteen. A
# probe now passes ONLY when the output carries the engine's own role-refusal
# signature; a usage error or a crash is reported as an INVALID PROBE, which
# is a defect in this script, not a boundary breach — and it is never silent.
set -u

export BRAIN_VAULT="${BRAIN_VAULT:-$PWD/vault}"
export BRAIN_ROLE=vm
export BRAIN_RUNTIME_DIR="${BRAIN_RUNTIME_DIR:-$BRAIN_VAULT/.brain}"
export BRAIN_MODEL_CACHE="$BRAIN_RUNTIME_DIR/model"
export PYTHONPATH="$BRAIN_RUNTIME_DIR/engine:$BRAIN_RUNTIME_DIR/vendor/$(uname -m):${PYTHONPATH:-}"
export PATH="$BRAIN_RUNTIME_DIR:$PATH"

# The engine's own words when the role gate fires. Anything else is not a
# refusal, whatever the exit code says.
REFUSAL_RE='is host-broker only|RoleError|role=vm is read\+draft'

BREACHED=0        # the boundary itself failed
INVALID=0         # this script asked the wrong question
OBSERVED=0        # true-but-expected conditions (see section B)
say(){ echo "$@"; }

must_refuse(){
  local label="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  say "-- $label"
  say "   \$ $*"
  printf '%s\n' "$out" | sed 's/^/   | /'
  if [ $rc -eq 0 ]; then
    say "   FAIL — command SUCCEEDED; the boundary is not enforced"
    BREACHED=$((BREACHED+1))
  elif printf '%s' "$out" | grep -qE "$REFUSAL_RE"; then
    say "   PASS — refused by the role gate (exit $rc)"
  elif [ -z "$out" ]; then
    say "   INVALID PROBE — non-zero exit, no message (crash, not a refusal)"
    INVALID=$((INVALID+1))
  else
    say "   INVALID PROBE — non-zero exit but no role-refusal signature; this"
    say "                   probe never reached the gate (usage error?). FIX THE"
    say "                   PROBE — it proves nothing either way."
    INVALID=$((INVALID+1))
  fi
  say ""
}

# AGENTS.md §9 + src/brain/cos.py `_PERMS`: `.brain/` is host-only BY CONTRACT
# and "never indexed", but the Cowork workspace is mounted wholesale over
# VirtioFS, which "may only partially honour POSIX bits". So a readable
# host-private file is an EXPECTED condition on this platform, not a breach —
# scoring it as one made this script report BREACHED on a healthy system. It is
# reported as an observation; §B2 carries the check that actually binds.
observe_readability(){
  local label="$1" path="$2"
  say "-- $label"
  say "   \$ head -c 1 $path"
  if head -c 1 "$path" >/dev/null 2>&1; then
    say "   OBSERVED: READABLE over the mount — expected on VirtioFS; the"
    say "             host/VM split is enforced behaviourally (§A, §B2), not by"
    say "             file permissions. A VM session must not read it."
    OBSERVED=$((OBSERVED+1))
  else
    say "   not readable (POSIX bits held on this mount)"
  fi
  say ""
}

say "=== VM boundary probe — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
say "vault=$BRAIN_VAULT  role=$BRAIN_ROLE  host=$(uname -a)"
say "engine: $(brain --version 2>&1)"
say ""
case "$(uname -s)" in
  Darwin) say "!! WARNING: this is a macOS host, not the Cowork Linux VM. This run"
          say "!! validates the SCRIPT and the engine's role gate ONLY. It proves"
          say "!! NOTHING about the physical host/VM boundary."; say "";;
esac

say "### A. host-broker verbs must ALL refuse BEFORE any mutation"
must_refuse "index commit"          brain sync
must_refuse "signed write"          brain write brain/resources/vm-boundary-probe.md --content "probe"
must_refuse "supersede"             brain supersede vm-boundary-a vm-boundary-b
must_refuse "ingest drain"          brain ingest
must_refuse "transcript ingest"     brain ingest-transcript /nonexistent.md \
                                        --origin verbal
must_refuse "link-discovery build"  brain graphify
must_refuse "cos run manifest"      brain cos-run-begin --run-id 2026-01-01-run1 \
                                        --lane codex-automation
must_refuse "cos broker fold"       brain cos-broker
must_refuse "cos corpus check"      brain cos-corpus-check --run-id 2026-01-01-run1
must_refuse "cos corpus append"     brain cos-corpus-append --run-id 2026-01-01-run1 \
                                        --bodyless "<probe@example.com>"
must_refuse "cos corpus close"      brain cos-corpus-close --run-id 2026-01-01-run1
must_refuse "cos corpus reopen"     brain cos-corpus-reopen --run-id 2026-01-01-run1
must_refuse "cos correction store"  brain cos-correct --round 1 --msg-key probe \
                                        --bucket NOISE --tier P3
must_refuse "cos evidence"          brain cos-evidence verify
must_refuse "cos priority map"      brain cos-priority-map
must_refuse "cos calibration report" brain cos-report
must_refuse "cos commitment spine"  brain cos-spine radar
must_refuse "cos hold"              brain cos-hold list
must_refuse "cos downloads sweep"   brain cos-ingest-sweep
must_refuse "audit chain query"     brain verify-audit
must_refuse "index rebuild"         brain rebuild
must_refuse "snapshot publish"      brain snapshot
must_refuse "maintenance umbrella"  brain maintain
must_refuse "owner inbox"           brain inbox
must_refuse "retro fold"            brain retro
must_refuse "filtered projection"   brain project --dest /tmp/vm-boundary-probe-dest

say "### B1. host-private state: readability over the mount (observation only)"
observe_readability "owner inbox queue"      "$BRAIN_RUNTIME_DIR/memory/inbox.jsonl"
observe_readability "broker claims ledger"   "$BRAIN_RUNTIME_DIR/cos/host/proposals/claims.jsonl"
observe_readability "broker autocap config"  "$BRAIN_RUNTIME_DIR/cos/host/autocap-config.json"
observe_readability "session handoff"        "$BRAIN_RUNTIME_DIR/memory/handoff.md"

say "### B2. host-private state must not be reachable through ANY read verb"
# This is the check that binds: `.brain/` is never indexed, so no retrieval
# surface can surface it even though the mount exposes the bytes.
#
# The token is DERIVED from a real host-private file at run time, never a
# guessed marker string: a hand-picked word like `provenance.trust` also
# appears in legitimate `raw/` notes, so it reports a leak that is not one
# (measured 2026-07-31: 19 innocent hits). A broker batch id exists in
# `.brain/` and nowhere else.
#
# The VERDICT is "did any hit come FROM `.brain/`", never "were there zero
# hits": `brain grep` is a tokenised index scan, not `grep(1)`, so a batch id
# `cosb-bc1603503f13` also matches ordinary notes that merely contain `cosb`
# (measured 2026-07-31: 4 innocent hits, none of them from `.brain/`). Counting
# hits reports a leak that is not one; reading the paths cannot.
leak_probe(){
  local label="$1" token="$2" leaked
  say "-- retrieval leak probe: $label"
  if [ -z "$token" ]; then
    if [ $OBSERVED -eq 0 ]; then
      # §B1 found NOTHING host-private readable, so the token could not be
      # derived — a STRONGER result than this probe can give, not a gap. It
      # must not be scored as an invalid probe, or a tighter mount would
      # report a worse verdict than a loose one.
      say "   N/A — no host-private file is readable from here at all (see §B1);"
      say "         nothing to leak-test, and that is the stronger outcome"
    else
      say "   SKIPPED — the file is readable but carries no token of this shape,"
      say "             so this probe proves nothing (not scored as a pass)"
      INVALID=$((INVALID+1))
    fi
    return
  fi
  leaked="$(brain grep "$token" --json 2>&1 \
    | tr ',' '\n' | grep '"path"' | grep '/\.brain/' || true)"
  if [ -z "$leaked" ]; then
    say "   PASS — a token that exists ONLY under .brain/ returns no hit FROM"
    say "          .brain/ (the runtime dir is not indexed)"
  else
    printf '%s\n' "$leaked" | sed 's/^/   | /'
    say "   FAIL — a read verb surfaced host-private state"
    BREACHED=$((BREACHED+1))
  fi
}
BATCH_TOKEN="$(grep -ho 'cosb-[0-9a-f]\{12\}' \
  "$BRAIN_RUNTIME_DIR/cos/host/proposals/batches.jsonl" \
  "$BRAIN_RUNTIME_DIR/memory/inbox.jsonl" 2>/dev/null | head -1)"
CLAIM_TOKEN="$(grep -ho '[0-9a-f]\{40,\}' \
  "$BRAIN_RUNTIME_DIR/cos/host/proposals/claims.jsonl" 2>/dev/null | head -1)"
leak_probe "broker batch id"      "$BATCH_TOKEN"
leak_probe "broker claim digest"  "$CLAIM_TOKEN"
say ""

say "### B3. the broker APPROVED QUEUE must not exist on this mount at all"
# INT-01. Between the owner's accept and the audited signature, the accepted
# bytes wait in a queue whose sha anchor the VM must not be able to reach. That
# is a LOCATION property (the host app-data dir, off this mount), so it is
# MEASURED here from a real VM session rather than asserted in a unit test that
# shares the host filesystem.
#
# A path check, not a write check: if any part of the queue is visible here,
# the property has already failed — writing to it would only prove it twice.
say "-- approved-queue payloads/anchors reachable from the VM"
QUEUE_HITS="$( { find "$BRAIN_VAULT" -name '*.anchor.json' 2>/dev/null
                 find "$BRAIN_VAULT" -type d -name 'cos-approved' 2>/dev/null
                 find "$BRAIN_VAULT/.." -maxdepth 2 -type d -name 'cos-approved' 2>/dev/null
               } | sort -u )"
if [ -z "$QUEUE_HITS" ]; then
  say "   PASS — no approved-queue payload or anchor is reachable from the VM"
  say "          (it lives in the host app-data dir, off this mount)"
else
  printf '%s\n' "$QUEUE_HITS" | sed 's/^/   | /'
  say "   FAIL — the accept-to-signing queue is on the shared mount; one write"
  say "          could substitute BOTH the approved bytes and their sha record"
  BREACHED=$((BREACHED+1))
fi
say ""
# The other half: the capture-inbox is still VM-writable (by design), and a
# draft dropped there under an ACCEPTED id must no longer be able to stand in
# for it. The VM cannot run the drain, so what is checked here is the property
# the VM CAN see: an accepted candidate is not waiting in capture-inbox.
say "-- accepted candidates must not be waiting in the VM-writable capture-inbox"
PENDING_ACCEPTED="$(grep -l 'cos_ingestion_batch' \
  "$BRAIN_RUNTIME_DIR/cos/host/proposals/batches.jsonl" 2>/dev/null || true)"
if [ -z "$PENDING_ACCEPTED" ]; then
  say "   N/A — no broker batch state is readable from here (stronger outcome)"
else
  ACCEPTED_IDS="$(sed -n 's/.*"state": *"consumed".*/&/p' \
    "$BRAIN_RUNTIME_DIR/cos/host/proposals/batches.jsonl" 2>/dev/null \
    | grep -o '"id": *"[a-z0-9-]*"' | sed 's/.*"\([a-z0-9-]*\)"$/\1/' | sort -u)"
  STRAY=""
  for id in $ACCEPTED_IDS; do
    [ -e "$BRAIN_RUNTIME_DIR/capture-inbox/$id.md" ] && STRAY="$STRAY $id"
  done
  if [ -z "$STRAY" ]; then
    say "   PASS — no owner-accepted id is parked in capture-inbox/"
  else
    say "   FAIL — accepted id(s) waiting in the VM-writable inbox:$STRAY"
    BREACHED=$((BREACHED+1))
  fi
fi
say ""

say "### C. no signing key may resolve"
say "-- signing-key resolution"
KEY_OUT="$(python3 -c '
from brain.audit import resolve_signing_key
try:
    k = resolve_signing_key()
    print("RESOLVED" if k else "none")
except Exception as e:
    print(f"{type(e).__name__}: {e}")
' 2>&1)"
printf '%s\n' "$KEY_OUT" | sed 's/^/   | /'
case "$KEY_OUT" in
  *RESOLVED*)
    say "   FAIL — a signing key resolved on the VM leg"; BREACHED=$((BREACHED+1));;
  *ModuleNotFoundError*|*ImportError*|*"No module named"*|*"not found"*|*"No such file"*)
    # The frozen-ELF lane ships neither an importable `brain` package nor,
    # necessarily, a `python3`. Either way this probe never ran, and scoring
    # it PASS would be a vacuous all-clear.
    say "   INCONCLUSIVE — the engine (or python3) is not available from this"
    say "                  shell, so the key path was never exercised. §A's"
    say "                  'signed write' refusal is the behavioural equivalent."
    INVALID=$((INVALID+1));;
  *)  say "   PASS — no signing key";;
esac
say ""

say "### D. the two things a VM legitimately CAN do must still work"
say "-- snapshot read"
# --no-rerank (RK-02 caller policy, 2026-08-04): a liveness check on the
# snapshot read path — it discards stdout entirely, so paying for BR-03's
# default-on cross-encoder here buys literally nothing.
if brain search "${VM_PROBE_TERM:-project}" --no-rerank --json >/dev/null 2>&1; then
  say "   PASS — snapshot search returned"
else
  say "   FAIL — the VM cannot read the snapshot (boundary is over-tight)"
  BREACHED=$((BREACHED+1))
fi
say "-- proposal drop (unsigned, into a dir \`sync\` never reads)"
# This leaves a real file in the drop dir for as long as it takes to delete it.
# The host broker fold runs hourly, so the window is tiny but not zero — the id
# and title are deliberately unmistakable, and a failed cleanup is reported
# loudly rather than left for the owner to find as a mystery batch item.
PROBE_ID="vm-boundary-probe-delete-me"
DROP_OUT="$(printf -- '---\nid: %s\ntitle: "VM boundary probe — safe to reject"\ntype: note\nclassification: Internal\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\nAutomated boundary probe. Not real content. Reject.\n' "$PROBE_ID" \
  | brain cos-propose --id "$PROBE_ID" --json 2>&1)"
printf '%s\n' "$DROP_OUT" | sed 's/^/   | /'
case "$DROP_OUT" in
  *dropped*) say "   PASS — unsigned drop accepted, nothing signed";;
  *)         say "   FAIL — the VM cannot even drop a proposal"; BREACHED=$((BREACHED+1));;
esac
# Delete the file the engine actually reported, not one this script guesses at
# — a guessed path that has drifted cleans up nothing and says it did.
PROBE_FILE="$(printf '%s' "$DROP_OUT" | sed -n 's/.*"proposal"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
[ -n "$PROBE_FILE" ] || PROBE_FILE="$BRAIN_RUNTIME_DIR/cos/drop/proposal-drop/$PROBE_ID.md"
rm -f "$PROBE_FILE" 2>/dev/null
if [ -e "$PROBE_FILE" ]; then
  say "   FAIL — could not clean up $PROBE_FILE; delete it before the next"
  say "          broker fold or it becomes an owner-batch question"
  BREACHED=$((BREACHED+1))
else
  say "   cleanup: probe drop removed ($PROBE_FILE)"
fi
say ""

say "=== SUMMARY ==="
say "  boundary failures : $BREACHED"
say "  invalid probes    : $INVALID   (this script asked the wrong question)"
say "  observations      : $OBSERVED   (host-private bytes readable over the mount)"
if [ $BREACHED -eq 0 ] && [ $INVALID -eq 0 ]; then
  say "=== VERDICT: BOUNDARY INTACT ==="
  exit 0
fi
if [ $BREACHED -eq 0 ]; then
  say "=== VERDICT: INCONCLUSIVE — $INVALID probe(s) never reached the gate ==="
  exit 2
fi
say "=== VERDICT: BREACHED — $BREACHED probe(s) failed ==="
exit 1
