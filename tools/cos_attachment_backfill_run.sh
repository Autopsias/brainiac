#!/bin/bash
# THE LAUNCH LINE FOR THE ATT-04 BACKFILL — copied from the lane that works,
# not hand-typed. Every executable line below is lifted from `cos_nightly.sh`
# (the block and line numbers are named beside each one) so a paid run cannot
# differ from the nightly's own by a typo.
#
# RUN THIS FROM THE MAIN SESSION ONLY. It reads and fetches from the LIVE
# mailbox. A subagent must not.
#
#   tools/cos_attachment_backfill_run.sh            # the real thing
#   BACKFILL_DRY=1 tools/cos_attachment_backfill_run.sh   # plan only, no fetch
#
# WHAT IT DOES NOT DO: it stops at the sweep. The bytes land in the host-private
# attachment quarantine at `state: pending`, which is where the owner's own
# verdict lane picks them up. Nothing here signs a note or writes to the vault.
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

PY="${PY:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY="$HOME/DeveloperFolder/profile-a-brain/.venv/bin/python"
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"

BRAIN_VAULT="${BRAIN_VAULT:-$HOME/DeveloperFolder/Brainiac/vault}"
export BRAIN_VAULT

# THE STAGING DIR IS READ OFF THE JOB, never typed (cos_nightly.sh:1368-1375).
# `cos_attachment_fetch.staging_dir` refuses ~/Downloads and refuses an unset
# value, so a wrong one stops the run instead of staging where nothing claims.
if [ -z "${BRAIN_COS_DOWNLOADS_DIR:-}" ]; then
  BRAIN_COS_DOWNLOADS_DIR="$(plutil -extract \
      EnvironmentVariables.BRAIN_COS_DOWNLOADS_DIR raw -o - \
      "$HOME/Library/LaunchAgents/com.brainiac.cos-nightly.plist" 2>/dev/null || true)"
fi
[ -n "${BRAIN_COS_DOWNLOADS_DIR:-}" ] || {
  echo "BRAIN_COS_DOWNLOADS_DIR is unset and the nightly plist does not carry" \
       "one. REFUSING: bytes would stage where the sweep never looks." >&2
  exit 2
}
export BRAIN_COS_DOWNLOADS_DIR
echo "staging dir: $BRAIN_COS_DOWNLOADS_DIR (recovered from the nightly job)"

EVID="${BACKFILL_EVIDENCE_DIR:-$HOME/DeveloperFolder/profile-a-brain/_plans/porter-finishes-2026-09-04/_evidence/porter-finishes}"
BACKFILL_RUN_ID="${BACKFILL_RUN_ID:-$(date -u +%Y-%m-%d)-backfill1}"

# --- 1. THE PLAN LOCK, before the first live call (s01's acquirer) -----------
# Released on EVERY exit — success, failure, or Ctrl-C — which is the whole
# reason it is a trap and not a line at the bottom.
"$PY" tools/cos_plan_lock.py acquire
trap '"$PY" tools/cos_plan_lock.py release || true' EXIT

# --- 2. THE PLAN, against the newest ledger already on disk ------------------
# `plan --against` needs an INGESTION LEDGER (it reads attachment ids), and only
# a full read pass writes one. So the batch is planned against the newest ledger
# the nightly already produced — no body is opened here, and nothing live runs.
PLAN_RUN="${PLAN_RUN:-$("$PY" - <<'PYEOF'
import os, pathlib
ops = pathlib.Path(os.environ["BRAIN_VAULT"]) / "cos-ops"
led = sorted(ops.glob("_cos_ingestion_ledger_*.jsonl"))
print(led[-1].name[len("_cos_ingestion_ledger_"):-len(".jsonl")] if led else "")
PYEOF
)}"
[ -n "$PLAN_RUN" ] || { echo "no ingestion ledger to plan against" >&2; exit 2; }
echo "planning against ledger: $PLAN_RUN"

"$PY" tools/cos_attachment_backfill.py --vault "$BRAIN_VAULT" \
    plan --against "$PLAN_RUN" --json "$EVID/attachment-backfill-plan.json"

if [ "${BACKFILL_DRY:-0}" = "1" ]; then
  echo "BACKFILL_DRY=1 — planned only, nothing live ran."; exit 0
fi

# --- 3. ARM THE TAB AND READ THE SIGN-IN STATE, before ANY live call ------
# cos_nightly.sh:907 arms BEFORE its enumeration at :1071; the first cut of this
# script armed after, and on 2026-09-05 the enumeration hit `FindItem ... http
# 401` on a signed-out tab with no verdict to say so. Read the arm VERDICT, not
# its exit code, exactly as cos_nightly.sh:2136-2160 does: `not-signed-in` is
# an owner action (sign in once inside the ego `cos` space), never a retry.
if [ "${COS_TRANSPORT:-ego}" = "ego" ]; then
  PREP="$("$PY" tools/cos_ego_arm.py 2>&1)" || true
  echo "arm: $(printf '%s' "$PREP" | tr -d '\n')"
  if printf '%s' "$PREP" | grep -q '"status": *"not-signed-in"'; then
    echo "the mailbox session has lapsed (arm status not-signed-in). Nothing" \
         "live ran. Sign in once inside the ego cos space, then re-run." >&2
    exit 4
  fi
fi

# --- 4. THE CHEAP LIVE RE-READ, immediately before the fetch -----------------
# cos_nightly.sh:1071 verbatim, minus the log redirect. `--enumerate-only` is
# pass 1 alone and writes NO ledger, NO contract and NO corpus, so it opens no
# bodies — it exists to answer exactly the two questions the checkpoint leaves
# open: is each planned thread still in the Inbox, and did it take a newer
# message while the batch waited for the GO. A dead attachment id is named by
# the fetch itself, as `fetch-failed` carrying the server's own reason.
TFLAG="--ego"
[ "${COS_TRANSPORT:-ego}" = "ego" ] || TFLAG="--cdp"
ENUMERATION_OUT="${ENUMERATION_OUT:-$EVID/attachment-backfill-enumeration.json}"
$PY tools/cos_driver.py $TFLAG --enumerate-only --out "$ENUMERATION_OUT" \
    || { echo "the enumeration stopped — see $ENUMERATION_OUT" >&2; exit 6; }

# --- 5. FETCH + SWEEP --------------------------------------------------------
# Re-arm immediately before the leg that drives megabytes through the renderer
# (cos_nightly.sh:2224-2226: run 185 died four minutes after a clean fetch
# because nothing re-armed between legs).
if [ "${COS_TRANSPORT:-ego}" = "ego" ]; then
  "$PY" tools/cos_ego_arm.py
fi
"$PY" tools/cos_attachment_backfill.py --vault "$BRAIN_VAULT" \
    apply --plan "$EVID/attachment-backfill-plan.json" \
    --run-id "$BACKFILL_RUN_ID" --verify-enumeration "$ENUMERATION_OUT" \
    --json "$EVID/attachment-backfill-result.json"

# --- 6. READ THE SIGNED NOTE IDS BACK ----------------------------------------
# Reports what the acceptance lane has actually minted so far. A row with no
# note id is bytes in quarantine awaiting the owner's verdict — a true state,
# not a failure, and re-runnable any time after the verdict lane has run.
"$PY" tools/cos_attachment_backfill.py --vault "$BRAIN_VAULT" \
    verify --result "$EVID/attachment-backfill-result.json" \
    --json "$EVID/attachment-backfill-verified.json"
