#!/bin/bash
# THE LAUNCH LINE FOR THE READ-THREAD ARCHIVE PASS (ZERO-01, s07).
#
# RUN THIS FROM THE MAIN SESSION ONLY, with the owner's in-session GO. It
# MUTATES the live mailbox. A subagent must not run it.
#
#   tools/cos_read_archive_run.sh              # census, the night, census
#   READ_ARCHIVE_DRY=1 tools/cos_read_archive_run.sh    # census only, nothing live
#
# WHY IT IS A WRAPPER AND NOT A BATCH OF ITS OWN. The thing that clears a
# `read` thread is a JUDGMENT pass — the archive claim is the judge's, and s07
# changed the judge (the P0 blast floor and the prompt that offers the lane).
# MEASURED 2026-09-05: replaying the FIXED planner over 2026-09-05-run260's
# finished ledger plans the same archives it already applied and not one more,
# because that ledger's claims were made under the OLD prompt. So there is no
# standalone batch to apply; the batch IS a night, and `cos_nightly.sh` is the
# lane that runs one. Everything this pass needs is already inside it: the
# fresh enumeration, the per-mutation re-resolve against a mailbox that moved,
# the undo row before the call, the per-verb caps, the kill switch and E1-E10.
#
# THE PLAN LOCK, AND WHY THE CHILD MUST NOT SEE IT. `cos_nightly.sh` DEFERS
# (exit 22) when a plan session's lock is held — that is how a plan session
# stops the 02:00 night from writing under it. This script is that plan
# session AND it runs the night, so it takes the lock at the DEFAULT path (the
# scheduled night reads that one and defers) and hands the child its own empty
# path through the documented `COS_PLAN_LOCK_FILE` override. Without the
# override the child would defer to us and nothing would run.
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

PY="${PY:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY="$HOME/DeveloperFolder/profile-a-brain/.venv/bin/python"
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"

BRAIN_VAULT="${BRAIN_VAULT:-$HOME/DeveloperFolder/Brainiac/vault}"
export BRAIN_VAULT
EVID="${READ_ARCHIVE_EVIDENCE_DIR:-$HOME/DeveloperFolder/profile-a-brain/_plans/porter-finishes-2026-09-04/_evidence/porter-finishes}"

# --- 0. THE CENSUS BEFORE, off the newest ledger already on disk -------------
# Nothing live runs here; it reads the ledger the last night wrote.
"$PY" tools/cos_read_census.py --vault "$BRAIN_VAULT" \
    --json "$EVID/read-threads-census-BEFORE.json"

if [ "${READ_ARCHIVE_DRY:-0}" = "1" ]; then
  echo "READ_ARCHIVE_DRY=1 — census only, nothing live ran."; exit 0
fi

# --- 1. THE PLAN LOCK, before the first live call (s01's acquirer) -----------
# Released on EVERY exit — success, failure, or Ctrl-C — which is the whole
# reason it is a trap and not a line at the bottom.
"$PY" tools/cos_plan_lock.py acquire
trap '"$PY" tools/cos_plan_lock.py release || true' EXIT

# --- 2. ARM THE TAB AND READ THE SIGN-IN STATE, before ANY live call --------
# Read the arm VERDICT, not its exit code (cos_nightly.sh:2136-2160):
# `not-signed-in` is an owner action — sign in once inside the ego `cos`
# space — never a retry. The night arms again for itself at :907; this one is
# here so a lapsed session costs a second rather than a whole read pass.
if [ "${COS_TRANSPORT:-ego}" = "ego" ]; then
  PREP="$("$PY" tools/cos_ego_arm.py 2>&1)" || true
  echo "arm: $(printf '%s' "$PREP" | tr -d '\n')"
  if printf '%s' "$PREP" | grep -q '"status": *"not-signed-in"'; then
    echo "the mailbox session has lapsed (arm status not-signed-in). Nothing" \
         "live ran. Sign in once inside the ego cos space, then re-run." >&2
    exit 4
  fi
fi

# --- 3. THE NIGHT ------------------------------------------------------------
# The child gets its own (absent) lock path so it does not defer to the lock we
# are holding on its behalf. Everything else is the nightly's own contract.
LOCK_FOR_CHILD="$(mktemp -u "${TMPDIR:-/tmp}/cos-plan-lock-child.XXXXXX")"
set +e
# COS_INGEST_BRIDGE=1 IS LOAD-BEARING, and its absence is what made the first
# two nights archive one thread each (2026-09-05). The bridge is DEFAULT OFF
# (cos_nightly.sh:1982). With it off the night judges, selects candidates and
# signs NOTHING, so RULE 1's substance gate refuses every archive - correctly.
# The bridge block sits after the judgment leg and before the mutation lane, so
# one night with this set ingests, signs, and then archives. The repo's own
# backfill gate (check_backfill_batch_reports.py:49) already requires it.
COS_INGEST_BRIDGE="${COS_INGEST_BRIDGE:-1}" \
COS_PLAN_LOCK_FILE="$LOCK_FOR_CHILD" tools/cos_nightly.sh
NIGHT_RC=$?
set -e
echo "cos_nightly.sh exit=$NIGHT_RC (0 done; 4 signed out; 13/14/16 partial" \
     "apply; 18/19/20 ingest or attachment stop — read the run report)"

# --- 4. THE CENSUS AFTER, off the ledger the night just wrote ----------------
"$PY" tools/cos_read_census.py --vault "$BRAIN_VAULT" \
    --json "$EVID/read-threads-census-AFTER.json"
exit "$NIGHT_RC"
