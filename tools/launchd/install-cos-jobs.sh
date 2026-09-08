#!/usr/bin/env bash
# Render the four COS templates into ~/Library/LaunchAgents. Deliberately does
# not call launchctl; loading is the attended first-night action.
set -eu

REPO="${COS_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PY="${COS_PYTHON:-$(command -v python3)}"
VAULT="${BRAIN_VAULT:-$HOME/DeveloperFolder/Brainiac/vault}"
# AN UNSET BRAIN_VAULT MAY NOT INVENT A VAULT (2026-09-06). This script used to
# fall through to the default path and then `mkdir -p` it a few lines below, so
# running it from a shell with no BRAIN_VAULT rendered ALL THREE jobs against a
# brand-new EMPTY directory and created it on the way out. Measured that day on
# this host: the owner ran the installer to add the read-back job, and it
# rewrote a working cos-nightly plist away from the live vault and onto this
# default, which had not existed one second earlier. Nothing failed and nothing warned — launchd kept running the
# correct job from memory, and the wrong file would have taken over at the next
# reboot, working an empty vault and reporting a quiet zero. That is the exact
# silent-absence class the $DOWNLOADS rule below exists to stop.
#
# So: creating the vault is allowed only when the caller NAMED it. An unset
# BRAIN_VAULT is accepted only if the default vault is already there.
if [ -z "${BRAIN_VAULT:-}" ] && [ ! -d "$VAULT" ]; then
  echo "install-cos-jobs: BRAIN_VAULT is unset and the default vault $VAULT does not exist." >&2
  echo "install-cos-jobs: refusing to render three jobs against a vault this script would have to invent." >&2
  echo "install-cos-jobs: name the vault, e.g. BRAIN_VAULT=\$HOME/DeveloperFolder/<name>/vault bash \$0" >&2
  exit 2
fi
LOG_DIR="${BRAIN_LOG_DIR:-$HOME/.brain/logs}"
DOWNLOADS="${BRAIN_COS_DOWNLOADS_DIR:-}"

# The rendered jobs carry NO BRAIN_INDEX_DIR since 2026-08-31: the old
# default pinned the app-data base, which shadows every per-vault index with
# one shared stale index.sqlite (see the template comment). Unpinned, the
# job resolves the same per-vault index every other host context uses.
if [ -n "${BRAIN_INDEX_DIR:-}" ]; then
  echo "install-cos-jobs: WARNING: BRAIN_INDEX_DIR is set in this shell but is no longer rendered into the jobs; the nightly resolves the per-vault index. Pin it in EVERY context for this vault, or nowhere." >&2
fi

# BRAIN_COS_DOWNLOADS_DIR is OPTIONAL since 2026-09-05. Unset, the fetch lane
# (`cos_ctl.sh`) and the engine's sweep (`cos._constants
# .DEFAULT_INGEST_SWEEP_DOWNLOADS_DIR`) BOTH default to ~/.brain/cos-downloads,
# which is one directory by construction. Set, it must be set for BOTH — the
# four-day silent failure this replaced was a maintain job that named a
# directory the fetch lane never wrote to, and one half of a pair is worse than
# neither.
#
# THIS SCRIPT FIXES ONE HALF, AND CANNOT SEE THE OTHER (adversarial review,
# 2026-09-05). It renders com.brainiac.cos-remind and com.brainiac.cos-nightly
# only. The job that SWEEPS — com.brainiac.nightly.<id>, which runs `brain
# maintain` — is written elsewhere and is neither rendered nor inspected here,
# so running this installer on an already-diverged host repoints the FETCH job
# and leaves the maintain job pointing wherever it pointed before. The message
# below says "the same path cos_ctl.sh and the engine default to", which is
# true of what this script writes and is NOT an all-clear for the pair. The
# engine is what catches the pair: the sweep reports the divergence
# (`misconfigured` in its report, an `action_required` row carrying a
# notify_key, so it reaches `brain alerts`) instead of a quiet zero.
if [ -z "$DOWNLOADS" ]; then
  DOWNLOADS="$HOME/.brain/cos-downloads"
  echo "install-cos-jobs: BRAIN_COS_DOWNLOADS_DIR unset; using the shared default $DOWNLOADS (the same path cos_ctl.sh and the engine default to)" >&2
fi
case "$VAULT:$LOG_DIR:$DOWNLOADS:$REPO:$PY" in
  *$'\n'*) echo "install-cos-jobs: paths may not contain newlines" >&2; exit 2 ;;
esac

DST="$HOME/Library/LaunchAgents"
# $DOWNLOADS IS CREATED, NOT JUST NAMED (review 2026-09-05). The sweep
# disables itself when its staging directory does not exist, so a fresh
# install that rendered the shared default into both jobs and then left
# the directory uncreated would fetch nothing and report a quiet zero —
# the same silent-absence failure the divergence check above replaced.
mkdir -p "$DST" "$LOG_DIR" "$VAULT/.brain/cos/sheets" "$DOWNLOADS"
# 0700, NOT THE UMASK (adversarial review, 2026-09-05). $DOWNLOADS holds fetched
# email attachments in the host home. `mkdir -p` inherits the shell umask, which
# is 022 on this machine, so the directory came out drwxr-xr-x — while the rest
# of the lane is explicit the other way (the quarantine and the settlements dir
# are both 0700). A staging directory for someone's mail is not world-readable.
chmod 700 "$DOWNLOADS"

render() {
  SRC="$1"; OUT="$2"
  "$PY" - "$SRC" "$OUT" "$HOME" "$REPO" "$PY" "$VAULT" "$LOG_DIR" "$DOWNLOADS" <<'PY'
import os
import plistlib
import sys
import tempfile
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
keys = ("__HOME__", "__COS_REPO__", "__PYTHON__", "__BRAIN_VAULT__",
        "__LOG_DIR__", "__DOWNLOADS_DIR__")
text = src.read_text(encoding="utf-8")
for key, value in zip(keys, sys.argv[3:]):
    text = text.replace(key, value)
if any(key in text for key in keys):
    raise SystemExit(f"unrendered placeholder remains in {src}")
plistlib.loads(text.encode("utf-8"))
fd, raw = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
tmp = Path(raw)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out)
finally:
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
PY
}

render "$REPO/tools/launchd/com.brainiac.cos-remind.plist" \
       "$DST/com.brainiac.cos-remind.plist"
render "$REPO/tools/launchd/com.brainiac.cos-nightly.plist" \
       "$DST/com.brainiac.cos-nightly.plist"
render "$REPO/tools/launchd/com.brainiac.cos-readback.plist" \
       "$DST/com.brainiac.cos-readback.plist"
render "$REPO/tools/launchd/com.brainiac.cos-sheet-ready.plist" \
       "$DST/com.brainiac.cos-sheet-ready.plist"

echo "Rendered four unloaded COS jobs in $DST"
echo "No launchctl command was run."
