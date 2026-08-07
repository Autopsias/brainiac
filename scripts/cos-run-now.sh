#!/usr/bin/env bash
# Fire the COS nightly run ON DEMAND, using the EXACT prompt, model, effort and
# working directory the 19:00 scheduled automation uses.
#
# WHY THIS EXISTS. Waiting until 19:00 to see whether a change worked is a
# 24-hour feedback loop. This reads `~/.codex/automations/cos/automation.toml`
# and runs the same thing now, so a manual run and the scheduled run cannot
# drift apart — the moment you hand-retype the prompt, you are testing
# something other than what ships.
#
# THIS IS NOT A DRY RUN. It reads real mail and, when the auto-archive overlay
# is enabled, MOVES real mail (capped, P0/P1 excluded, every row undoable). It
# never sends. Use --extract-only to suppress the mutating lanes for a test.
#
# Usage:
#   scripts/cos-run-now.sh                 # full run, exactly as scheduled
#   scripts/cos-run-now.sh --extract-only  # ingestion only; archive/marks off
#   scripts/cos-run-now.sh --show          # print what WOULD run, run nothing
set -euo pipefail

AUTOMATION="${HOME}/.codex/automations/cos/automation.toml"
# The automation config names the workspace; the vault comes from the env.
VAULT="${BRAIN_VAULT:-}"
# Derive the vault from the automation's own workspace when unset, so this
# script carries no deployment-specific path of its own.
OVERLAY=""

# A full run is 20-40 minutes of real mail work, so it must outlive the shell
# that started it. `--background` is not a convenience: a foreground run killed
# by a terminal timeout dies MID-PHASE, and a half-finished triage is worse
# than one that never started.
BACKGROUND=0
MODE="full"
for arg in "$@"; do
  case "$arg" in
    --extract-only) MODE="extract-only" ;;
    --show)         MODE="show" ;;
    --background|-b) BACKGROUND=1 ;;
    "")             ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$BACKGROUND" == "1" ]]; then
  LOG="${TMPDIR:-/tmp}/cos-run-$(date +%Y%m%dT%H%M%S).log"
  # Re-exec ourselves WITHOUT --background, detached, logging everything.
  ARGS=(); [[ "$MODE" == "extract-only" ]] && ARGS+=(--extract-only)
  nohup "$0" "${ARGS[@]}" > "$LOG" 2>&1 &
  echo "COS run started in the background (pid $!)"
  echo "  mode : $MODE"
  echo "  log  : $LOG"
  echo
  echo "Follow it:      tail -f $LOG"
  echo "Is it alive?    ps -p \$! >/dev/null && echo running || echo finished"
  echo "  (do NOT pgrep for 'codex exec' — other sessions on this"
  echo "   machine run codex too, and you will watch the wrong process)"
  echo "When it ends:   ./scripts/cos-check-run.sh"
  exit 0
fi

[[ -f "$AUTOMATION" ]] || { echo "no automation config at $AUTOMATION" >&2; exit 1; }

read -r PROMPT MODEL EFFORT CWD STATUS <<<"$(python3 - "$AUTOMATION" <<'PY'
import sys, tomllib, pathlib, base64
d = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())
# base64 the prompt: it is multi-KB and full of quotes/newlines.
print(base64.b64encode(d["prompt"].encode()).decode(),
      d.get("model", "gpt-5.6-sol"),
      d.get("reasoning_effort", "high"),
      (d.get("cwds") or [str(pathlib.Path.cwd())])[0],
      d.get("status", "UNKNOWN"))
PY
)"
PROMPT="$(printf '%s' "$PROMPT" | base64 --decode)"

echo "COS run — from the SCHEDULED automation's own config"
echo "  status : $STATUS"
echo "  model  : $MODEL (reasoning: $EFFORT)"
echo "  cwd    : $CWD"
echo "  vault  : $VAULT"
echo "  mode   : $MODE"

# What the mutating lanes will do, read from the LIVE overlay rather than
# assumed — the difference between "archives nothing" and "archives a backlog"
# is one line in a file, and it has silently flipped before.
if [[ -z "$VAULT" && -d "$CWD/vault" ]]; then VAULT="$CWD/vault"; fi
OVERLAY="${VAULT:+${VAULT}/overlay/cos/auto-archive.md}"
if [[ -n "$OVERLAY" && -f "$OVERLAY" ]]; then
  echo "  auto-archive overlay:"
  # Settings block ONLY — stop at the first prose heading. The owner-ruling
  # prose below quotes these key names, and echoing a quoted line as if it
  # were a live setting is how you misreport what the run will do.
  awk '/^## /{exit} /^(enabled|scope|aged_read_lane|aged_read_min_days|any_sender_lane|chip_reeval):/{print "      " $0}' \
      "$OVERLAY"
else
  echo "  auto-archive overlay: ABSENT (per-key defaults apply — see the template)"
fi

if [[ "$MODE" == "show" ]]; then
  echo
  echo "--- prompt (${#PROMPT} chars) ---"
  printf '%s\n' "$PROMPT"
  exit 0
fi

if [[ "$MODE" == "extract-only" ]]; then
  # Additive instruction, appended AFTER the shipped prompt so the doctrine it
  # overrides is still the doctrine that ships. Suppressing the mutating lanes
  # is a TEST posture, not a second code path.
  PROMPT="${PROMPT}

=== TEST RUN OVERRIDE (operator, on-demand) ===
This is an ON-DEMAND EXTRACTION TEST, not the scheduled run.
DO NOT MUTATE THE MAILBOX. Specifically: archive NOTHING (treat the
auto-archive lane as kill-switched for this run and report it as
'would-archive-only'), change NO read/unread state beyond what reading a
message already-marked read implies, apply NO chips, and compose NO drafts.
Everything else runs exactly as written: full triage, Phase 1.6 ingestion with
the category taxonomy, the ingestion ledger, cos-propose for every candidate,
the metrics row and the outcome contract. State plainly in the run report that
this was an extraction-only test run and which lanes were suppressed."
  echo
  echo "  NOTE: mailbox-mutating lanes suppressed for this run (archive/marks/chips/drafts)."

  # A MEASUREMENT run may need to evaluate every eligible read thread rather
  # than the first 20: an `over-cap` row says nothing about what that thread
  # contained, so an unobserved queue can never explain a missing lift. Opt-in
  # only, and only on the non-mutating lane.
  if [[ -n "${COS_BODY_OPEN_CAP:-}" ]]; then
    PROMPT="${PROMPT}

MEASUREMENT CAP OVERRIDE (operator, this run only): Phase 1.6 rule 1½'s
body-open cap is raised from 20 to ${COS_BODY_OPEN_CAP} opens so that every
eligible ALREADY-READ in-scope thread is evaluated instead of left unobserved.
UNCHANGED and still binding: IsRead is screened FIRST from the list, an UNREAD
thread is NEVER opened, the open order is P0 -> P1 -> act, and every ledger row
still carries body_opened. Use held_reason 'over-cap' only if the RAISED cap
still binds, and report the actual open count."
    echo "  NOTE: body-open cap raised to ${COS_BODY_OPEN_CAP} for this measurement run."
  fi
fi

# STA-01: the HOST freezes the run manifest BEFORE the run starts — run id,
# the SKILL.md that will execute + its digest, and both producer versions. That
# record is what stamps this run's candidates at claim time; without it every
# candidate is quarantined (unattributable), so a failure here aborts the
# launch rather than starting a run whose output cannot be claimed.
BRAIN_RUN_BEGIN="${BRAIN_BIN:-brain}"
if ! RUN_JSON="$("$BRAIN_RUN_BEGIN" ${VAULT:+--vault "$VAULT"} cos-run-begin --json)"; then
  echo "cos-run-begin FAILED — refusing to launch a run whose candidates could" >&2
  echo "not be attributed to a bundle. Fix the deployment lane first." >&2
  exit 1
fi
RUN_ID="$(printf '%s' "$RUN_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')"
echo "  run id : $RUN_ID (host-assigned; the run reads it from"
echo "           <ops>/shared/current-run.json and names its artifacts after it)"

# The run resolves `brain` from ITS OWN login-shell PATH, which is NOT this
# shell's PATH — measured: prepending a directory here leaves the run on
# ~/.local/bin/brain. So when the operator pins an engine with BRAIN_BIN (the
# same one cos-run-begin just used to freeze the manifest), SAY SO in the
# prompt, or the manifest is frozen by one engine while the candidates are
# staged by another — the exact split that leaves a finding unattributable.
if [[ -n "${BRAIN_BIN:-}" && "${BRAIN_BIN}" != "brain" ]]; then
  export BRAIN_BIN
  PROMPT="${PROMPT}

ENGINE PIN (operator, this run only): the host froze this run's manifest with
the engine at \$BRAIN_BIN (exported in your environment). Invoke the brain CLI
as \"\$BRAIN_BIN\" everywhere the skill says \`brain\` — including the
cos-propose capability probe and every cos-propose call — so the engine that
stages the candidates is the engine that froze the manifest. A bare \`brain\`
resolves to a DIFFERENT install here and its candidates cannot be joined."
  echo "  engine : $BRAIN_BIN (pinned for the run, not just for cos-run-begin)"
fi

echo
echo "Starting. This is a real run against real mail; it never sends."

# The prompt goes in on STDIN via `-`, not as an argv positional: it is ~3.7 KB
# of quotes, newlines and backticks, and passing it as an argument made codex
# fall through to "Reading additional input from stdin..." and hang.
#
# `-C` rather than `cd`: codex resolves its working root from the flag, and the
# workspace is NOT a git repo (it is a mail/ops workspace, not source), so
# `--skip-git-repo-check` is required or codex refuses with "Not inside a
# trusted directory". Sandbox stays whatever ~/.codex/config.toml sets
# (workspace-write) — this script never widens it.
# Pass the attachment staging dir the SCHEDULED job has in its plist. Without
# it a manual run reports `attachment_lane: blocked-no-downloads-mount` and
# looks like a lane defect when it is only a missing env var — measured on
# run 59.
if [[ -z "${BRAIN_COS_DOWNLOADS_DIR:-}" ]]; then
  BRAIN_COS_DOWNLOADS_DIR="$(plutil -extract EnvironmentVariables.BRAIN_COS_DOWNLOADS_DIR raw -o - \
      "$HOME"/Library/LaunchAgents/com.brainiac.nightly.*.plist 2>/dev/null | head -1)"
  [[ -n "$BRAIN_COS_DOWNLOADS_DIR" ]] && export BRAIN_COS_DOWNLOADS_DIR \
    && echo "  downloads dir: $BRAIN_COS_DOWNLOADS_DIR (from the nightly job)"
fi

printf '%s' "$PROMPT" | exec codex exec \
  --model "$MODEL" \
  -c "model_reasoning_effort=$EFFORT" \
  -C "$CWD" \
  --skip-git-repo-check \
  -
