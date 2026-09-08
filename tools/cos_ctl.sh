#!/usr/bin/env bash
# cos-ctl — the ONE control surface for the COS nightly. Nothing to memorise:
#
#   tools/cos_ctl.sh status      what is scheduled, what ran last, what applied
#   tools/cos_ctl.sh page        build the HTML status page and open it
#   tools/cos_ctl.sh run [all]   a full nightly, NOW (reads, judges, applies)
#   tools/cos_ctl.sh run cap N   ATTENDED: freeze the plan, show every proposed
#                                archive, PAUSE for your GO, then apply THAT
#                                plan. REFUSES the whole lane (nothing
#                                dispatched) if it would archive more than N —
#                                an attended cap stops and comes back to you,
#                                it never archives a truncated prefix
#   tools/cos_ctl.sh dry [all]   a full nightly that stops before the apply
#                                `all` = historic sweep (lift the recency window)
#   tools/cos_ctl.sh stop        halt the run in flight + pause the schedule
#   tools/cos_ctl.sh resume      re-arm after a stop (schedule back on)
#   tools/cos_ctl.sh undo [ID]   put a run's archives back (default: last run)
#   tools/cos_ctl.sh unchip [ID] take a run's priority chips back off (ditto)
#   tools/cos_ctl.sh discard-drafts [ID]
#                                discard only that run's verified signed drafts
#   tools/cos_ctl.sh install     WRITE the rendered plist(s) to
#                                ~/Library/LaunchAgents, then PRINT the
#                                `launchctl load` command to run yourself
#   tools/cos_ctl.sh uninstall   PRINT the two commands that remove it
#
# `install` WRITES the plist file(s) (refusing first if run from a plan
# worktree — see the `install)` case) but never calls `launchctl`; `uninstall`
# only prints. Loading/unloading persistent automation is the owner's action,
# always.
set -u

# Derived from this file's own location, never from a worktree literal — the
# nightly's rule and the nightly's reason (review 2026-08-12): this script sets
# PYTHONPATH and picks the plist source, so a REPO that disagrees with where the
# script lives runs one tree's control surface over another tree's engine.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
# THE DERIVATION IS ONLY HALF THE GUARD, and this file got only that half
# (review 2026-08-12). `cos_nightly.sh` also SELF-ASSERTS the path it derived
# and REFUSES a `COS_REPO` that disagrees with it; without both, a derivation
# that silently produced the wrong tree — or an environment naming another one
# — runs this control surface, and its PYTHONPATH and its plist source, over a
# different tree's engine. The two scripts hold the same rule or they are two
# rules.
if [ -z "$REPO" ] || [ ! -f "$REPO/tools/cos_ctl.sh" ]; then
  echo "cos-ctl: cannot locate its own checkout from ${BASH_SOURCE[0]}" >&2
  exit 2
fi
if [ -n "${COS_REPO:-}" ] && [ "$COS_REPO" != "$REPO" ]; then
  echo "cos-ctl: COS_REPO=$COS_REPO but this script lives in $REPO — one
 checkout, or the control surface drives another tree's engine" >&2
  exit 2
fi
export BRAIN_VAULT="${BRAIN_VAULT:-$HOME/DeveloperFolder/Brainiac/vault}"
export PYTHONPATH="$REPO/src"
PY="${COS_PYTHON:-python3}"
# ONE template, rendered by ONE script (`tools/launchd/install-cos-jobs.sh`).
# Until 2026-09-04 `install` carried its OWN copy at
# `tools/com.brainiac.cos-nightly.plist` plus its own sed-based render — a
# second renderer of the same job is exactly the "two templates that can
# disagree" failure this line exists to close: that copy had drifted (no
# caffeinate, no --batches, none of COS_BODY_CAP/COS_SINCE_DAYS/
# COS_INGEST_BRIDGE/BRAIN_REQUIRE_REAL_EMBEDDER, a 06:30 schedule nobody
# runs) from what is actually installed. `PLIST_SRC` now names the SAME
# template `install-cos-jobs.sh` renders, so `install` delegates instead of
# re-implementing.
PLIST_SRC="$REPO/tools/launchd/com.brainiac.cos-nightly.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.brainiac.cos-nightly.plist"
LABEL="com.brainiac.cos-nightly"
LOG_DIR="${BRAIN_LOG_DIR:-$HOME/.brain/logs}"

cd "$REPO" || { echo "no repo at $REPO" >&2; exit 2; }

current_run() {
  $PY -c "
from brain import cos; import json, pathlib
p = cos.current_run_path(pathlib.Path('$BRAIN_VAULT'))
print(json.loads(p.read_text())['run_id'] if p.exists() else '')" 2>/dev/null
}

case "${1:-status}" in

  status)
    $PY tools/cos_status_page.py --text
    ;;

  page)
    OUT="$($PY tools/cos_status_page.py)" || exit 1
    echo "$OUT"
    open "$(printf '%s' "$OUT" | tail -1)"
    ;;

  run)
    # `run cap N` is the ATTENDED lane: it needs a terminal, because the pause
    # reads your GO from stdin and refuses when there is nobody there.
    if [ "${2:-}" = "cap" ]; then
      [ -n "${3:-}" ] || { echo "cos-ctl: 'run cap' needs a number, e.g. cos_ctl.sh run cap 20" >&2; exit 2; }
      exec bash tools/cos_nightly.sh "--archive-cap=$3"
    fi
    # `run all` lifts the recency window (historic sweep of the whole mailbox).
    [ "${2:-}" = "all" ] && exec bash tools/cos_nightly.sh --all
    exec bash tools/cos_nightly.sh
    ;;

  dry)
    [ "${2:-}" = "all" ] && exec bash tools/cos_nightly.sh --dry --all
    exec bash tools/cos_nightly.sh --dry
    ;;

  stop)
    RUN="$(current_run)"
    if [ -n "$RUN" ]; then
      # The emergency brake cos_mutate re-reads BETWEEN mutations: the pass
      # stops after the one in flight, never mid-request.
      $PY -c "
import sys; sys.path.insert(0,'tools')
import cos_mutate as cm; from pathlib import Path
p = cm.stop_file(Path('$BRAIN_VAULT'), '$RUN'); p.touch()
print(f'stop file down for {\"$RUN\"}: {p}')"
    fi
    if [ -f "$PLIST_DST" ]; then
      launchctl unload "$PLIST_DST" 2>/dev/null && echo "schedule PAUSED ($LABEL unloaded)"
    else
      echo "no schedule installed — nothing to pause"
    fi
    echo "kill switch (persists across runs): set 'enabled: false' in"
    echo "  $BRAIN_VAULT/overlay/cos/auto-archive.md"
    ;;

  resume)
    RUN="$(current_run)"
    if [ -n "$RUN" ]; then
      $PY -c "
import sys; sys.path.insert(0,'tools')
import cos_mutate as cm; from pathlib import Path
p = cm.stop_file(Path('$BRAIN_VAULT'), '$RUN')
p.unlink(missing_ok=True); print(f'stop file lifted for {\"$RUN\"}')"
    fi
    if [ -f "$PLIST_DST" ]; then
      launchctl load "$PLIST_DST" 2>/dev/null && echo "schedule ON ($LABEL loaded)"
    else
      echo "no schedule installed — run: tools/cos_ctl.sh install"
    fi
    ;;

  undo)
    RUN="${2:-$(current_run)}"
    [ -n "$RUN" ] || { echo "no run id — pass one: cos_ctl.sh undo 2026-08-11-run122" >&2; exit 2; }
    $PY tools/cos_cdp_capture.py --prepare >/dev/null || {
      echo "the automation browser is not ready — sign in to Chrome-COS and retry" >&2; exit 4; }
    $PY tools/cos_mutate.py undo --run-id "$RUN" --cdp
    ;;

  unchip)
    # The chip lane's reversal, the same shape as `undo`: uncapped chips are only
    # safe while taking them off is one command (FINDING 2026-08-12).
    RUN="${2:-$(current_run)}"
    [ -n "$RUN" ] || { echo "no run id — pass one: cos_ctl.sh unchip 2026-08-11-run122" >&2; exit 2; }
    $PY tools/cos_cdp_capture.py --prepare >/dev/null || {
      echo "the automation browser is not ready — sign in to Chrome-COS and retry" >&2; exit 4; }
    $PY tools/cos_mutate.py unchip --run-id "$RUN" --cdp
    ;;

  discard-drafts)
    # THE NIGHTLY RUNS THIS ITSELF now, after its apply (owner ruling
    # 2026-08-28: one draft per thread, and it is the newest). This entry point
    # remains the ATTENDED REPAIR for the night that could not — the log names
    # it when the discard fails. The page half re-reads each exact ledgered item
    # in Drafts immediately before its draft-only captured DeleteItem replay.
    # The source run SELECTS THE THREADS while every run ledger is scanned for
    # both the survivor and the losers; there is no prefix/limit approximation.
    RUN="${2:-$(current_run)}"
    [ -n "$RUN" ] || { echo "no run id — pass one: cos_ctl.sh discard-drafts 2026-08-11-run122" >&2; exit 2; }
    MANIFEST="$BRAIN_VAULT/cos-ops/_cos_draft_discard_manifest.json"
    $PY tools/cos_mutate.py discard-draft-manifest --run-id "$RUN" \
      --out "$MANIFEST" || exit $?
    $PY tools/cos_ego_arm.py >/dev/null || {
      echo "the ego mail task space is not armed — sign in and retry" >&2; exit 4; }
    $PY tools/cos_mutate.py discard-drafts --run-id "$RUN" \
      --discard-manifest "$MANIFEST" --ego
    ;;

  install)
    # REFUSE from a plan worktree (2026-09-04 review finding). `REPO` is
    # derived from where THIS script lives (top of file); a plan worktree
    # disappears the moment its branch merges, so an install run from one
    # bakes ProgramArguments pointing at a path that is about to vanish —
    # exactly the bug cos_nightly_schedule_check.py exists to catch, caught
    # here BEFORE it ever reaches a rendered plist. Run install from the
    # stable deploy worktree instead (see cos_deploy_promote.py).
    case "$REPO" in
      */.plan-worktrees/*)
        echo "REFUSING: cos_ctl.sh install would render a plist pointing at" >&2
        echo "  $REPO" >&2
        echo "which is a PLAN WORKTREE — it disappears when its branch merges," >&2
        echo "silencing the job with no error anywhere. Run 'install' from the" >&2
        echo "stable deploy worktree instead (advance it with" >&2
        echo "tools/cos_deploy_promote.py, see that script's docstring)." >&2
        exit 1 ;;
    esac
    # The tracked plist is a TEMPLATE (`__HOME__`, `__COS_REPO__`, …): a
    # tracked file may not bake in the operator's home path, or it reaches
    # the public export — tests/test_export_cleanroom.py. Lint the source
    # here so a malformed template is caught before anything renders.
    plutil -lint "$PLIST_SRC" >/dev/null || {
      echo "REFUSING: $PLIST_SRC is not a well-formed plist" >&2; exit 1; }
    # `plutil -lint` IS NOT ENOUGH. It accepted a template whose XML comment
    # carried a double hyphen -- illegal in XML -- which every strict parser
    # rejects; launchd took the file, plistlib could not read it back, and the
    # verification that was supposed to confirm the install died instead
    # (measured 2026-08-12). So the strict parser gets a vote too.
    $PY - "$PLIST_SRC" <<'PLCHK' || exit 1
import plistlib, sys
try:
    plistlib.load(open(sys.argv[1], "rb"))
except Exception as exc:
    sys.exit(f"REFUSING: {sys.argv[1]} is not well-formed for a STRICT parser "
             f"({exc}). plutil is lenient; a double hyphen inside an XML "
             f"comment is the usual cause.")
PLCHK
    # THE INTERPRETER IS PROVEN HERE, NOT ASSUMED. launchd's PATH resolves
    # `python3` to the brainiac ENGINE venv, which carries no `websockets`, so
    # the browser lane dies at --prepare and the night exits 5 (measured
    # 2026-08-12, the first run ever started through launchd). Every hand-run
    # passed because a terminal resolves a different python. So the plist NAMES
    # the interpreter, and this refuses to print an install for one that cannot
    # actually run the lane.
    PY_ABS="$(command -v "$PY")" || { echo "REFUSING: no $PY on PATH" >&2; exit 1; }
    "$PY_ABS" -c 'import websockets' 2>/dev/null || {
      echo "REFUSING: $PY_ABS cannot import websockets, which the CDP browser" >&2
      echo "lane needs. Installing the schedule against it would give you a job" >&2
      echo "that exits 5 every morning. Fix with:" >&2
      echo "  $PY_ABS -m pip install websockets" >&2
      echo "or point COS_PYTHON at an interpreter that has it, then re-run." >&2
      exit 1; }
    # DELEGATE to the ONE renderer (`tools/launchd/install-cos-jobs.sh`) —
    # this used to carry its own sed one-liner against the SECOND, staler
    # template `install` renamed away from above. It writes rendered files
    # only; it never calls launchctl.
    export COS_PYTHON="$PY_ABS"
    export BRAIN_COS_DOWNLOADS_DIR="${BRAIN_COS_DOWNLOADS_DIR:-$HOME/.brain/cos-downloads}"
    bash "$REPO/tools/launchd/install-cos-jobs.sh" || exit 1
    echo
    echo "Run this command yourself (loading a schedule is an owner action):"
    echo
    echo "  launchctl load '$PLIST_DST'"
    echo
    echo "Read the schedule back with 'launchctl print', NEVER from the file —"
    echo "the file is what was just rendered and proves nothing about what"
    echo "launchd actually loaded:"
    echo "  launchctl print gui/\$(id -u)/$LABEL"
    ;;

  uninstall)
    echo "Run these two commands yourself:"
    echo
    echo "  launchctl unload '$PLIST_DST'"
    echo "  rm '$PLIST_DST'"
    ;;

  *)
    sed -n '2,29p' "$0"; exit 2 ;;
esac
